"""Q65–Q69 read-only operation recovery evidence inspection.

Pending, a recorded Checkpoint, and an unknown external outcome are separate.
No probe, Consumer hook, Job transition, write, or retry is performed here.
All observations are snapshot evidence, never an execution authorization.
"""

from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from ._canonical import checked_label
from .checkpoint_store import CheckpointIntegrityError, validate_checkpoint_row
from .deployment_compatibility import PreflightHoldReason, inspect_delivery_contracts_in_snapshot
from .immutable_intent import IntentIntegrityError, validate_intent_record
from .lineage_store import LineageHold, LineageIntegrityError, require_source_checkpoints, validate_lineage_row
from .operation_attempt import AttemptIntegrityError, has_attempt_store_shape, validate_attempt_row
from .operation_checkpoint import OperationCheckpoint
from .operation_contracts import ContractCatalog
from .operation_intent import OperationIntent
from .operation_lineage import OperationLineage
from .operation_plan import PlanStructure
from .sealed_plan import PlanIntegrityError, validate_sealed_plan_row

RecoveryStatus = Literal["pending_observed", "checkpoint_observed", "hold"]
RecoveryHoldReason = (
    PreflightHoldReason
    | Literal[
        "operation_missing",
        "job_state_unknown",
        "active_unresolved",
        "historical_start_unresolved",
        "claim_history_unresolved",
        "job_history_unresolved",
        "evidence_store_unavailable",
        "terminal_unresolved",
        "outcome_unknown",
        "checkpoint_invalid",
        "lineage_invalid",
        "intent_invalid",
        "attempt_invalid",
    ]
)


@dataclass(frozen=True, slots=True)
class OperationRecoveryObservation:
    event_id: int
    operation_id: str
    status: RecoveryStatus
    reason: RecoveryHoldReason | None
    structure_digest: str | None
    intent_digest: str | None
    checkpoint_digest: str | None
    attempt_digest: str | None = None


def _hold(
    event_id: int,
    operation_id: str,
    reason: RecoveryHoldReason,
    *,
    structure_digest: str | None = None,
    intent_digest: str | None = None,
    attempt_digest: str | None = None,
) -> OperationRecoveryObservation:
    return OperationRecoveryObservation(
        event_id, operation_id, "hold", reason, structure_digest, intent_digest, None, attempt_digest
    )


async def inspect_operation_recovery_evidence(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    catalog: ContractCatalog,
    event_id: int,
    operation_id: str,
) -> OperationRecoveryObservation:
    """Observe durable evidence in one REPEATABLE READ READ ONLY snapshot.

    A Job with no recorded Intent is 'pending_observed' only when status is
    todo, no execution or claim history, and no pending abort or stale worker.
    This classification does not establish an absence of remote effects.
    Once an Intent is committed without a Checkpoint the result is always
    unknown, irrespective of later Job state. No automatic retry is offered.
    """
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Recovery inspection requires an idle autocommit connection.")
    if type(catalog) is not ContractCatalog:
        raise TypeError("Expected an explicit ContractCatalog.")
    if type(event_id) is not int or event_id <= 0:
        raise ValueError("event_id must be positive.")
    checked_label(operation_id, "operation_id")

    async with connection.transaction():
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        preflight = await inspect_delivery_contracts_in_snapshot(connection, catalog, event_id)
        if preflight.status != "compatible":
            return _hold(event_id, operation_id, preflight.reason or "unsupported_contract")

        cursor = await connection.execute(
            """
            SELECT p.structure_version, p.structure_digest, p.structure_bytes,
                   j.status::text, j.worker_id, j.attempts, j.abort_requested,
                   EXISTS (
                       SELECT 1 FROM procrastinate_events AS e
                       WHERE e.job_id = j.id AND e.type::text = 'started'
                   ) AS was_started,
                   EXISTS (
                       SELECT 1 FROM github_delivery_claim_generations AS g
                       WHERE g.event_id = r.event_id
                   ) AS had_claim_generation
            FROM github_event_receipts AS r
            JOIN procrastinate_jobs AS j ON j.id = r.job_id
            JOIN github_sealed_plans AS p ON p.event_id = r.event_id
            WHERE r.event_id = %s
            """,
            (event_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return _hold(event_id, operation_id, "job_state_unknown")
        try:
            sealed = validate_sealed_plan_row(event_id, row[:3], "existing")
            plan = PlanStructure.from_canonical_bytes(sealed.structure_bytes)
        except PlanIntegrityError, TypeError, ValueError:
            return _hold(event_id, operation_id, "plan_invalid")
        op = next((item for item in plan.operations if item.operation_id == operation_id), None)
        if op is None:
            return _hold(
                event_id,
                operation_id,
                "operation_missing",
                structure_digest=sealed.structure_digest,
            )

        # Root-only persistence predates the opt-in Checkpoint/lineage DDL.
        # Never reference absent relations or treat uninspectable evidence as
        # a fresh pending operation. This remains a read-only observation.
        stores = await connection.execute(
            "SELECT to_regclass('github_operation_checkpoints') IS NOT NULL, "
            "to_regclass('github_operation_lineages') IS NOT NULL"
        )
        if await stores.fetchone() != (True, True):
            return _hold(event_id, operation_id, "evidence_store_unavailable", structure_digest=sealed.structure_digest)

        evidence_cursor = await connection.execute(
            """
            SELECT i.created_generation, i.structure_digest, i.intent_version,
                   i.intent_digest, i.intent_bytes,
                   c.recorded_generation, c.intent_digest, c.checkpoint_version,
                   c.checkpoint_digest, c.checkpoint_bytes,
                   l.lineage_version, l.lineage_digest, l.lineage_bytes
            FROM github_operation_intents AS i
            LEFT JOIN github_operation_checkpoints AS c
              ON (c.event_id, c.operation_id) = (i.event_id, i.operation_id)
            LEFT JOIN github_operation_lineages AS l
              ON (l.event_id, l.operation_id) = (i.event_id, i.operation_id)
            WHERE i.event_id = %s AND i.operation_id = %s
            """,
            (event_id, operation_id),
        )
        evidence = await evidence_cursor.fetchone()
        if evidence is None:
            status, job_worker, attempts, abort_requested, was_started, had_claim_generation = row[3:9]
            if had_claim_generation is True and was_started is False:
                return _hold(
                    event_id, operation_id, "claim_history_unresolved", structure_digest=sealed.structure_digest
                )
            if status == "todo" and was_started is False:
                if job_worker is not None or type(attempts) is not int or attempts != 0 or abort_requested is not False:
                    return _hold(
                        event_id, operation_id, "job_history_unresolved", structure_digest=sealed.structure_digest
                    )
                return OperationRecoveryObservation(
                    event_id, operation_id, "pending_observed", None, sealed.structure_digest, None, None
                )
            if status == "doing":
                reason: RecoveryHoldReason = "active_unresolved"
            elif was_started is True:
                reason = "historical_start_unresolved"
            elif status in ("succeeded", "failed", "aborted"):
                reason = "terminal_unresolved"
            else:
                reason = "job_state_unknown"
            return _hold(event_id, operation_id, reason, structure_digest=sealed.structure_digest)

        try:
            intent_record = validate_intent_record(event_id, operation_id, evidence[:5], "existing")
            intent = OperationIntent.from_canonical_bytes(intent_record.intent_bytes)
        except IntentIntegrityError, TypeError, ValueError:
            return _hold(event_id, operation_id, "intent_invalid", structure_digest=sealed.structure_digest)
        if intent_record.structure_digest != sealed.structure_digest or intent.operation != op:
            return _hold(event_id, operation_id, "intent_invalid", structure_digest=sealed.structure_digest)

        summary = {"structure_digest": sealed.structure_digest, "intent_digest": intent.intent_digest}

        if op.depends_on:
            if evidence[10] is None:
                return _hold(event_id, operation_id, "lineage_invalid", **summary)
            try:
                lineage_record = validate_lineage_row(event_id, operation_id, evidence[10:13], "existing")
                lineage = OperationLineage.from_canonical_bytes(lineage_record.lineage_bytes)
                await require_source_checkpoints(
                    connection=connection,
                    event_id=event_id,
                    expected_plan=plan,
                    intent=intent,
                    lineage=lineage,
                )
            except LineageHold, LineageIntegrityError, TypeError, ValueError:
                return _hold(event_id, operation_id, "lineage_invalid", **summary)
        elif any(value is not None for value in evidence[10:13]):
            return _hold(event_id, operation_id, "lineage_invalid", **summary)

        # v2 remote mutation evidence is optional until schema opt-in.
        # Once a v2 Intent exists, an uninspectable Attempt store must Hold:
        # absence is never interpreted as an unattempted POST.
        if (
            op.consumer_id == "github_issue"
            and op.operation_type == "add_label"
            and op.contract_version == "add_label:v2"
        ):
            if not await has_attempt_store_shape(connection):
                return _hold(event_id, operation_id, "evidence_store_unavailable", **summary)
            found = await connection.execute(
                """
                SELECT origin_generation, intent_digest, attempt_version, attempt_digest, attempt_bytes
                FROM github_operation_attempts WHERE event_id = %s AND operation_id = %s
                """,
                (event_id, operation_id),
            )
            raw_attempt = await found.fetchone()
            if raw_attempt is not None:
                try:
                    attempt = validate_attempt_row(event_id, operation_id, raw_attempt)
                except AttemptIntegrityError:
                    return _hold(event_id, operation_id, "attempt_invalid", **summary)
                if (
                    attempt.attempt.intent_digest != intent.intent_digest
                    or attempt.attempt.origin_generation != intent_record.created_generation
                ):
                    return _hold(event_id, operation_id, "attempt_invalid", **summary)
                summary["attempt_digest"] = attempt.attempt_digest

        if evidence[5] is None:
            if any(value is not None for value in evidence[6:10]):
                return _hold(event_id, operation_id, "checkpoint_invalid", **summary)
            return _hold(event_id, operation_id, "outcome_unknown", **summary)

        try:
            checkpoint = validate_checkpoint_row(event_id, operation_id, evidence[5:10], "existing")
            checkpoint_model = OperationCheckpoint.from_canonical_bytes(checkpoint.checkpoint_bytes)
        except CheckpointIntegrityError, TypeError, ValueError:
            return _hold(event_id, operation_id, "checkpoint_invalid", **summary)
        if checkpoint.intent_digest != intent.intent_digest or checkpoint_model.intent_digest != intent.intent_digest:
            return _hold(event_id, operation_id, "checkpoint_invalid", **summary)
        return OperationRecoveryObservation(
            event_id,
            operation_id,
            "checkpoint_observed",
            None,
            sealed.structure_digest,
            intent.intent_digest,
            checkpoint.checkpoint_digest,
            summary.get("attempt_digest"),
        )
