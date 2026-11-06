"""Read-only Q76 pre-deployment contract compatibility of persisted Deliveries.

Inspects explicit events or the bounded set of nonterminal local Jobs under a
REPEATABLE READ READ ONLY transaction. It does not claim/transition Jobs,
install DDL, contact GitHub, invoke Consumer hooks, or authorize execution.
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from local_dev_mcp.tools.gh.webhook import supports_target_action

from .checkpoint_store import CheckpointIntegrityError, validate_checkpoint_row
from .immutable_intent import IntentIntegrityError, validate_intent_record
from .lineage_store import LineageHold, LineageIntegrityError, require_source_checkpoints, validate_lineage_row
from .operation_contracts import ContractCatalog, ContractObservation
from .operation_intent import OperationIntent
from .operation_lineage import OperationLineage
from .operation_plan import PlanStructure
from .schema import ENVELOPE_SCHEMA_VERSION
from .sealed_plan import PlanIntegrityError, validate_sealed_plan_row

PreflightHoldReason = Literal[
    "receipt_missing",
    "job_unlinked",
    "unsupported_contract",
    "plan_missing",
    "plan_invalid",
    "intent_invalid",
    "claim_evidence_invalid",
    "checkpoint_invalid",
    "lineage_invalid",
    "concurrency_unverified",
    "contract_mismatch",
    "scope_incomplete",
]


@dataclass(frozen=True, slots=True)
class DeliveryContractInspection:
    event_id: int
    status: Literal["compatible", "hold"]
    reason: PreflightHoldReason | None
    structure_digest: str | None
    operations: tuple[ContractObservation, ...]


@dataclass(frozen=True, slots=True)
class DeploymentPreflight:
    status: Literal["compatible", "hold"]
    reason: Literal["scope_incomplete"] | None
    inspected: tuple[DeliveryContractInspection, ...]
    total_inspected: int


def _require_idle(connection: AsyncConnection[tuple[Any, ...]]) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Deployment preflight requires an idle autocommit connection.")


def _hold(event_id: int, reason: PreflightHoldReason) -> DeliveryContractInspection:
    return DeliveryContractInspection(event_id, "hold", reason, None, ())


async def inspect_delivery_contracts_in_snapshot(
    connection: AsyncConnection[tuple[Any, ...]],
    catalog: ContractCatalog,
    event_id: int,
) -> DeliveryContractInspection:
    """Internal: caller already owns a read-only consistent snapshot."""
    cursor = await connection.execute(
        """
        SELECT r.job_id, r.event_schema_version, r.envelope_schema_version,
               r.target_kind, r.action, r.event_type, r.repository_id,
               r.source_instance_id, r.repository, r.target_number,
               r.target_updated_at,
               j.id, j.task_name, j.args,
               p.structure_version, p.structure_digest, p.structure_bytes
        FROM github_event_receipts AS r
        LEFT JOIN procrastinate_jobs AS j ON j.id = r.job_id
        LEFT JOIN github_sealed_plans AS p ON p.event_id = r.event_id
        WHERE r.event_id = %s
        """,
        (event_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return _hold(event_id, "receipt_missing")
    (
        job_id,
        event_version,
        envelope_version,
        kind,
        action,
        event_type,
        repository_id,
        source,
        repository,
        target_number,
        target_updated_at,
        actual_job,
        task_name,
        args,
        plan_version,
        plan_digest,
        plan_bytes,
    ) = row
    if (
        event_version != "github-webhook:1"
        or envelope_version != ENVELOPE_SCHEMA_VERSION
        or type(kind) is not str
        or type(action) is not str
        or not supports_target_action(kind, action)
        or event_type != f"{'issues' if kind == 'issue' else 'pull_request'}.{action}"
        or type(repository_id) is not int
        or repository_id <= 0
        or source != f"github:repository:{repository_id}"
        or type(repository) is not str
        or not repository
        or type(target_number) is not int
        or target_number <= 0
        or type(target_updated_at) is not str
        or not target_updated_at
    ):
        return _hold(event_id, "unsupported_contract")
    if (
        type(job_id) is not int
        or job_id <= 0
        or job_id != actual_job
        or type(task_name) is not str
        or not task_name
        or type(args) is not dict
        or args != {"event_id": event_id}
        or type(cast(dict[str, object], args).get("event_id")) is not int
    ):
        return _hold(event_id, "job_unlinked")
    if plan_version is None and plan_digest is None and plan_bytes is None:
        return _hold(event_id, "plan_missing")
    try:
        sealed = validate_sealed_plan_row(event_id, (plan_version, plan_digest, plan_bytes), "existing")
        plan = PlanStructure.from_canonical_bytes(sealed.structure_bytes)
    except PlanIntegrityError, ValueError, TypeError:
        return _hold(event_id, "plan_invalid")

    stores_cursor = await connection.execute(
        "SELECT to_regclass('github_operation_lineages') IS NOT NULL, "
        "to_regclass('github_operation_checkpoints') IS NOT NULL"
    )
    stores = await stores_cursor.fetchone()
    lineage_installed = stores is not None and stores[0] is True
    checkpoint_installed = stores is not None and stores[1] is True

    # Chronology is a Checkpoint integrity error even for a source that a
    # dependent lineage references. Classify it before lineage traversal,
    # preserving the preflight's stable checkpoint_invalid reason.
    if checkpoint_installed:
        chronology = await connection.execute(
            """
            SELECT 1 FROM github_operation_checkpoints AS c
            JOIN github_operation_intents AS i
              ON (i.event_id, i.operation_id) = (c.event_id, c.operation_id)
            WHERE c.event_id = %s AND c.recorded_generation < i.created_generation
            LIMIT 1
            """,
            (event_id,),
        )
        if await chronology.fetchone() is not None:
            return _hold(event_id, "checkpoint_invalid")

    intents_cursor = await connection.execute(
        """
        SELECT operation_id, created_generation, structure_digest,
               intent_version, intent_digest, intent_bytes
        FROM github_operation_intents
        WHERE event_id = %s ORDER BY operation_id
        """,
        (event_id,),
    )
    existing: list[OperationIntent] = []
    intent_generations: dict[str, int] = {}
    claim_generations: set[int] = set()
    nodes = {operation.operation_id: operation for operation in plan.operations}
    for raw_row in await intents_cursor.fetchall():
        operation_id = raw_row[0]
        if type(operation_id) is not str or operation_id not in nodes:
            return _hold(event_id, "intent_invalid")
        try:
            record = validate_intent_record(event_id, operation_id, raw_row[1:], "existing")
            decoded = OperationIntent.from_canonical_bytes(record.intent_bytes)
        except IntentIntegrityError, ValueError, TypeError:
            return _hold(event_id, "intent_invalid")
        if record.structure_digest != sealed.structure_digest or decoded.operation != nodes[operation_id]:
            return _hold(event_id, "intent_invalid")
        if decoded.operation.depends_on:
            # Both evidence tables are opt-in. A dependent Intent cannot be
            # deployment-compatible if their owners have not installed them.
            if not (lineage_installed and checkpoint_installed):
                return _hold(event_id, "lineage_invalid")
            cursor = await connection.execute(
                """
                SELECT lineage_version, lineage_digest, lineage_bytes
                FROM github_operation_lineages
                WHERE event_id = %s AND operation_id = %s
                """,
                (event_id, operation_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return _hold(event_id, "lineage_invalid")
            try:
                lineage_row = validate_lineage_row(event_id, operation_id, row, "existing")
                lineage = OperationLineage.from_canonical_bytes(lineage_row.lineage_bytes)
                await require_source_checkpoints(
                    connection=connection,
                    event_id=event_id,
                    expected_plan=plan,
                    intent=decoded,
                    lineage=lineage,
                )
            except LineageHold, LineageIntegrityError, ValueError, TypeError:
                return _hold(event_id, "lineage_invalid")
        elif lineage_installed:
            stray_cursor = await connection.execute(
                "SELECT 1 FROM github_operation_lineages WHERE event_id = %s AND operation_id = %s",
                (event_id, operation_id),
            )
            if await stray_cursor.fetchone() is not None:
                return _hold(event_id, "lineage_invalid")
        existing.append(decoded)
        intent_generations[operation_id] = record.created_generation
        claim_generations.add(record.created_generation)

    # The Checkpoint schema is opt-in for root-only evidence. If installed,
    # every persisted Checkpoint must still match a validated immutable Intent.
    # Missing source Checkpoint tables for dependent Intents already HOLD above.
    if checkpoint_installed:
        checkpoint_cursor = await connection.execute(
            """
            SELECT operation_id, recorded_generation, intent_digest,
                   checkpoint_version, checkpoint_digest, checkpoint_bytes
            FROM github_operation_checkpoints
            WHERE event_id = %s ORDER BY operation_id
            """,
            (event_id,),
        )
        intents_by_id = {item.operation.operation_id: item.intent_digest for item in existing}
        for raw_checkpoint in await checkpoint_cursor.fetchall():
            operation_id = raw_checkpoint[0]
            if type(operation_id) is not str or operation_id not in intents_by_id:
                return _hold(event_id, "checkpoint_invalid")
            try:
                checkpoint = validate_checkpoint_row(event_id, operation_id, raw_checkpoint[1:], "existing")
            except CheckpointIntegrityError, ValueError, TypeError:
                return _hold(event_id, "checkpoint_invalid")
            if (
                checkpoint.intent_digest != intents_by_id[operation_id]
                or checkpoint.recorded_generation < intent_generations[operation_id]
            ):
                return _hold(event_id, "checkpoint_invalid")
            claim_generations.add(checkpoint.recorded_generation)

    # Persisted origin references are not sufficient on their own: the
    # Generation FK does not constrain started_event_id to this Job or even
    # to a real started event. Validate only Generations referenced by
    # existing Intents/Checkpoints; unreferenced history belongs to recovery.
    if claim_generations:
        origins = await connection.execute(
            """
            SELECT g.generation, g.job_id, g.started_event_id, g.worker_id,
                   g.attempts, e.job_id, e.type::text
            FROM github_delivery_claim_generations AS g
            LEFT JOIN procrastinate_events AS e ON e.id = g.started_event_id
            WHERE g.event_id = %s AND g.generation = ANY(%s)
            """,
            (event_id, sorted(claim_generations)),
        )
        checked: set[int] = set()
        for generation, claim_job, started_id, worker_id, attempts, event_job, event_type in await origins.fetchall():
            if (
                type(generation) is not int
                or generation in checked
                or type(claim_job) is not int
                or claim_job != job_id
                or type(started_id) is not int
                or started_id <= 0
                or type(worker_id) is not int
                or worker_id <= 0
                or type(attempts) is not int
                or attempts < 0
                or event_job != job_id
                or event_type != "started"
            ):
                return _hold(event_id, "claim_evidence_invalid")
            checked.add(generation)
        if checked != claim_generations:
            return _hold(event_id, "claim_evidence_invalid")

    report = catalog.inspect_plan(plan, intents=tuple(existing))
    failing = next((item for item in report.operations if not item.compatible), None)
    return DeliveryContractInspection(
        event_id=event_id,
        status=report.status,
        reason=None if failing is None else failing.reason,
        structure_digest=sealed.structure_digest,
        operations=report.operations,
    )


async def inspect_persisted_delivery_contracts(
    *, connection: AsyncConnection[tuple[Any, ...]], catalog: ContractCatalog, event_id: int
) -> DeliveryContractInspection:
    """Inspect one known event, including failed/terminal Deliveries.

    The result is a historical snapshot only; no runtime operation is approved.
    """
    _require_idle(connection)
    if type(catalog) is not ContractCatalog:
        raise TypeError("Expected explicit ContractCatalog.")
    if type(event_id) is not int or event_id <= 0:
        raise ValueError("event_id must be positive.")
    async with connection.transaction():
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return await inspect_delivery_contracts_in_snapshot(connection, catalog, event_id)


async def inspect_pending_deployment_contracts(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    catalog: ContractCatalog,
    max_events: int = 500,
) -> DeploymentPreflight:
    """Bounded scan of all locally nonterminal Receipt Jobs in one snapshot.

    A large or incomplete scope returns HOLD rather than a partial success.
    Failed/terminal Jobs remain inspectable by explicit event ID when an
    operator considers their manual recovery. This is not a deployment gate.
    """
    _require_idle(connection)
    if type(catalog) is not ContractCatalog:
        raise TypeError("Expected explicit ContractCatalog.")
    if type(max_events) is not int or not 1 <= max_events <= 10_000:
        raise ValueError("max_events must be between 1 and 10000.")
    async with connection.transaction():
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor = await connection.execute(
            """
            SELECT r.event_id FROM github_event_receipts AS r
            LEFT JOIN procrastinate_jobs AS j ON j.id = r.job_id
            WHERE j.id IS NULL OR j.status::text NOT IN ('succeeded', 'failed', 'aborted')
            ORDER BY r.event_id LIMIT %s
            """,
            (max_events + 1,),
        )
        rows = await cursor.fetchall()
        if len(rows) > max_events:
            return DeploymentPreflight("hold", "scope_incomplete", (), 0)
        inspected = tuple(
            [await inspect_delivery_contracts_in_snapshot(connection, catalog, event_id) for (event_id,) in rows]
        )
    return DeploymentPreflight(
        status="compatible" if all(item.status == "compatible" for item in inspected) else "hold",
        reason=None,
        inspected=inspected,
        total_inspected=len(inspected),
    )
