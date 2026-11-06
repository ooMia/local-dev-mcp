"""Opt-in two-Operation Procrastinate Worker: Issue read -> additive Label goal.

The caller explicitly owns the App, a single pinned label, read/write
transports, two dedicated database sessions and Worker lifecycle. Never
auto-registers or retries after unknown GitHub effects.
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from procrastinate import App, JobContext
from procrastinate.tasks import Task
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from ._canonical import checked_digest
from .checkpoint_store import load_operation_checkpoint, record_operation_checkpoint
from .claim_evidence import inspect_running_claim
from .claim_generation import record_claim_generation
from .claim_plan import inspect_claim_plan
from .immutable_intent import load_operation_intent, record_operation_intent
from .issue_label_consumer import (
    IssueAddLabel,
    bind_issue_add_label_contract,
    make_issue_add_label_intent,
    validate_issue_label,
)
from .issue_label_scope import PostgresIssueLabelWriteScope
from .issue_read_consumer import IssueReadTransport, bind_issue_read_contract, make_issue_read_intent
from .issue_read_readiness import inspect_issue_read_worker_schema
from .operation_attempt import has_attempt_store_shape
from .operation_checkpoint import OperationCheckpoint
from .operation_contracts import ContractCatalog
from .operation_plan import OperationNode, PlanStructure
from .receipts import load_target
from .sealed_plan import seal_plan_structure

ConnectionFactory = Callable[[], Awaitable[AsyncConnection[tuple[Any, ...]]]]
TASK_NAME = "github_issue_add_label_v2"
_READ = OperationNode("read", "github_issue", "read_current", "read:v1")
_LABEL = OperationNode("label", "github_issue", "add_label", "add_label:v2", ("read",))
_PLAN = PlanStructure((_READ, _LABEL))


class _ClaimArgs(TypedDict):
    connection: AsyncConnection[tuple[Any, ...]]
    event_id: int
    generation: int
    job_id: int
    expected_task_name: str
    worker_id: int
    attempts: int
    started_event_id: int
    expected_plan: PlanStructure


class IssueLabelWorkerHold(RuntimeError):
    """A new attempt, uncertain outcome or stale claim must be held."""


async def _require_label_evidence_schema(connection: AsyncConnection[tuple[Any, ...]]) -> None:
    """Read-only fail-closed preflight before writing even a root Intent."""
    ready = await inspect_issue_read_worker_schema(connection=connection)
    if not ready.ready or not await has_attempt_store_shape(connection):
        raise IssueLabelWorkerHold("label_evidence_schema_not_ready")

    # Lineage is mandatory for the dependent Intent. Check its append-only
    # identity contract before starting any remote GET or committing an Intent.
    structural = await connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS c
            WHERE c.oid = to_regclass('github_operation_lineages')
              AND c.relkind IN ('r', 'p')
              AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_constraint AS k
                  WHERE k.conrelid = c.oid AND k.contype = 'p'
                    AND pg_catalog.pg_get_constraintdef(k.oid) =
                        'PRIMARY KEY (event_id, operation_id)'
              )
              AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_constraint AS k
                  WHERE k.conrelid = c.oid AND k.contype = 'f'
                    AND k.conname = 'github_lineage_intent_fk'
                    AND k.confrelid = to_regclass('github_operation_intents')
                    AND pg_catalog.pg_get_constraintdef(k.oid) =
                        'FOREIGN KEY (event_id, operation_id) REFERENCES ' ||
                        'github_operation_intents(event_id, operation_id)'
              )
              AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_trigger AS t
                  JOIN pg_catalog.pg_proc AS fn ON fn.oid = t.tgfoid
                  WHERE t.tgrelid = c.oid
                    AND t.tgname = 'github_operation_lineage_immutable'
                    AND t.tgenabled IN ('O', 'A')
                    AND t.tgtype = 27
                    AND fn.proname = 'github_reject_operation_lineage_change'
                    AND fn.pronamespace = c.relnamespace
              )
        )
        """
    )
    if await structural.fetchone() != (True,):
        raise IssueLabelWorkerHold("label_evidence_schema_not_ready")
    cols = await connection.execute(
        """
        SELECT column_name, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'github_operation_lineages'
        """
    )
    actual = {name: (kind, nullable) for name, kind, nullable in await cols.fetchall()}
    required = {
        "event_id": ("int8", "NO"),
        "operation_id": ("text", "NO"),
        "lineage_version": ("text", "NO"),
        "lineage_digest": ("text", "NO"),
        "lineage_bytes": ("bytea", "NO"),
    }
    if any(actual.get(name) != expected for name, expected in required.items()):
        raise IssueLabelWorkerHold("label_evidence_schema_not_ready")


async def run_issue_label_claim(
    *,
    connection_factory: ConnectionFactory,
    observe: IssueReadTransport,
    add_label: IssueAddLabel,
    label: str,
    test_evidence_digest: str,
    event_id: int,
    job_id: int,
    worker_id: int,
    attempts: int,
    task_name: str = TASK_NAME,
) -> None:
    """One explicitly claimed two-node Plan; no resume or automatic retry.

    The root read commits first. Then the dependent Label Intent+lineage are
    sealed before Q78/Q79 authorize the sole possible additive POST. Any
    existing Intent prevents a fresh network call after a crash or re-claim.
    """
    if any(type(v) is not int or v <= 0 for v in (event_id, job_id, worker_id)):
        raise IssueLabelWorkerHold("invalid_job_context")
    if type(attempts) is not int or attempts < 0:
        raise IssueLabelWorkerHold("invalid_job_context")
    if type(task_name) is not str or not task_name.strip():
        raise IssueLabelWorkerHold("invalid_task_name")
    validate_issue_label(label)
    checked_digest(test_evidence_digest, "test_evidence_digest")

    async with await connection_factory() as connection:
        if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
            raise IssueLabelWorkerHold("requires_idle_autocommit_connection")
        target = await load_target(connection=connection, event_id=event_id)
        if target is None or target.kind != "issue":
            raise IssueLabelWorkerHold("unsupported_target")
        await _require_label_evidence_schema(connection)

        # An incomplete prior execution is never evidence that the write
        # was not attempted. Refuse any pre-existing root/dependent Intent
        # or Checkpoint before issuing a network request.
        for operation in (_READ, _LABEL):
            if await load_operation_intent(
                connection=connection, event_id=event_id, operation_id=operation.operation_id
            ):
                raise IssueLabelWorkerHold("intent_already_recorded")
            if await load_operation_checkpoint(
                connection=connection, event_id=event_id, operation_id=operation.operation_id
            ):
                raise IssueLabelWorkerHold("checkpoint_already_recorded")

        async with await connection_factory() as attempt_connection:
            if (
                not attempt_connection.autocommit
                or attempt_connection.info.transaction_status != TransactionStatus.IDLE
            ):
                raise IssueLabelWorkerHold("requires_idle_autocommit_connection")

            running = await inspect_running_claim(
                connection=connection,
                event_id=event_id,
                job_id=job_id,
                expected_task_name=task_name,
                worker_id=worker_id,
                attempts=attempts,
            )
            if running.status != "observed" or running.started_event_id is None:
                raise IssueLabelWorkerHold("claim_not_current")
            started = running.started_event_id
            claim = await record_claim_generation(
                connection=connection,
                event_id=event_id,
                job_id=job_id,
                expected_task_name=task_name,
                worker_id=worker_id,
                attempts=attempts,
                started_event_id=started,
            )
            await seal_plan_structure(connection=connection, event_id=event_id, plan=_PLAN)

            kwargs: _ClaimArgs = _ClaimArgs(
                connection=connection,
                event_id=event_id,
                generation=claim.generation,
                job_id=job_id,
                expected_task_name=task_name,
                worker_id=worker_id,
                attempts=attempts,
                started_event_id=started,
                expected_plan=_PLAN,
            )
            read_intent = make_issue_read_intent(operation=_READ, target=target)
            reader = bind_issue_read_contract(
                intent=read_intent,
                target=target,
                transport=observe,
                test_evidence_digest=test_evidence_digest,
            )
            if not ContractCatalog((reader,)).inspect(_READ, intent=read_intent).compatible:
                raise IssueLabelWorkerHold("read_contract_incompatible")
            created_read = await record_operation_intent(**kwargs, intent=read_intent)
            if created_read.disposition != "recorded":
                raise IssueLabelWorkerHold("intent_already_recorded")

            live = await inspect_claim_plan(**kwargs)
            if live.status != "observed":
                raise IssueLabelWorkerHold("claim_not_current")
            if (
                reader.execute is None
                or reader.decode is None
                or reader.verify is None
                or reader.concurrency.guard is None
            ):
                raise IssueLabelWorkerHold("read_contract_incomplete")
            await reader.concurrency.guard(read_intent)
            raw = await reader.execute(read_intent)
            if type(raw) is not bytes:
                raise IssueLabelWorkerHold("read_result_invalid")
            read_checkpoint = reader.decode(raw)
            if type(read_checkpoint) is not OperationCheckpoint:
                raise IssueLabelWorkerHold("read_checkpoint_invalid")

            # First GET cannot serve as a claim lease for the verification GET.
            live = await inspect_claim_plan(**kwargs)
            if live.status != "observed":
                raise IssueLabelWorkerHold("claim_not_current")
            if not await reader.verify(read_intent, read_checkpoint):
                raise IssueLabelWorkerHold("read_verification_failed")
            stored_read = await record_operation_checkpoint(**kwargs, checkpoint=read_checkpoint)
            if stored_read.disposition != "recorded":
                raise IssueLabelWorkerHold("read_checkpoint_already_recorded")

            label_intent, lineage = make_issue_add_label_intent(
                operation=_LABEL,
                target=target,
                label=label,
                source_intent=read_intent,
                source_checkpoint=read_checkpoint,
            )
            scope = PostgresIssueLabelWriteScope(
                connection=connection,
                attempt_connection=attempt_connection,
                target=target,
                event_id=event_id,
                generation=claim.generation,
                job_id=job_id,
                expected_task_name=task_name,
                worker_id=worker_id,
                attempts=attempts,
                started_event_id=started,
                expected_plan=_PLAN,
            )
            writer = bind_issue_add_label_contract(
                intent=label_intent,
                target=target,
                label=label,
                source_intent=read_intent,
                source_checkpoint=read_checkpoint,
                observe=observe,
                add_label=add_label,
                write_scope=scope,
                test_evidence_digest=test_evidence_digest,
            )
            if (
                not ContractCatalog((reader, writer))
                .inspect_plan(_PLAN, intents=(read_intent, label_intent))
                .compatible
            ):
                raise IssueLabelWorkerHold("label_contract_incompatible")
            created_label = await record_operation_intent(**kwargs, intent=label_intent, lineage=lineage)
            if created_label.disposition != "recorded":
                raise IssueLabelWorkerHold("intent_already_recorded")

            if writer.execute is None or writer.decode is None or writer.verify is None:
                raise IssueLabelWorkerHold("label_contract_incomplete")
            label_raw = await writer.execute(label_intent)
            if type(label_raw) is not bytes:
                raise IssueLabelWorkerHold("label_result_invalid")
            label_checkpoint = writer.decode(label_raw)
            if type(label_checkpoint) is not OperationCheckpoint:
                raise IssueLabelWorkerHold("label_checkpoint_invalid")
            if not await writer.verify(label_intent, label_checkpoint):
                raise IssueLabelWorkerHold("label_verification_failed")
            stored_label = await record_operation_checkpoint(**kwargs, checkpoint=label_checkpoint)
            if stored_label.disposition != "recorded":
                raise IssueLabelWorkerHold("label_checkpoint_already_recorded")


def register_issue_label_worker_task(
    *,
    app: App,
    connection_factory: ConnectionFactory,
    observe: IssueReadTransport,
    add_label: IssueAddLabel,
    label: str,
    test_evidence_digest: str,
    queue: str = "default",
) -> Task[..., Any, Any]:
    """Explicitly register a non-retrying Issue read->label Task.

    Neither registration nor import starts a Worker or performs a GET/POST.
    The owner pins the same label and injected transports across deployments.
    Job args remain the original {"event_id": id} minimal Envelope reference.
    """
    if not callable(connection_factory) or not callable(observe) or not callable(add_label):
        raise TypeError("Explicit database, GET and POST implementations are required.")
    if type(queue) is not str or not queue.strip():
        raise ValueError("queue must be nonempty")
    validate_issue_label(label)
    checked_digest(test_evidence_digest, "test_evidence_digest")

    @app.task(name=TASK_NAME, queue=queue, pass_context=True, retry=False)
    async def _label_task(context: JobContext, event_id: int) -> None:
        job = context.job
        if job.id is None or job.worker_id is None:
            raise IssueLabelWorkerHold("invalid_job_context")
        await run_issue_label_claim(
            connection_factory=connection_factory,
            observe=observe,
            add_label=add_label,
            label=label,
            test_evidence_digest=test_evidence_digest,
            event_id=event_id,
            job_id=job.id,
            worker_id=job.worker_id,
            attempts=job.attempts,
        )

    return _label_task
