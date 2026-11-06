"""Opt-in Procrastinate Worker binding for one read-only GitHub Issue Operation.

No default task or Worker registration. A caller explicitly owns the Procrastinate
App, database connection factory, transport, queue and worker lifecycle.
No mutation, Probe-only retry, or automatic resubmission is permitted.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from procrastinate import App, JobContext
from procrastinate.tasks import Task
from psycopg import AsyncConnection

from ._canonical import checked_digest
from .checkpoint_store import load_operation_checkpoint, record_operation_checkpoint
from .claim_evidence import inspect_running_claim
from .claim_generation import record_claim_generation
from .claim_plan import inspect_claim_plan
from .immutable_intent import load_operation_intent, record_operation_intent
from .issue_read_consumer import IssueReadTransport, bind_issue_read_contract, make_issue_read_intent
from .operation_contracts import ContractCatalog
from .operation_plan import OperationNode, PlanStructure
from .receipts import load_target
from .sealed_plan import seal_plan_structure

ConnectionFactory = Callable[[], Awaitable[AsyncConnection[tuple[Any, ...]]]]
TASK_NAME = "github_issue_read_current_v1"
_READ = OperationNode("read", "github_issue", "read_current", "read:v1")
_READ_PLAN = PlanStructure((_READ,))


class IssueReadWorkerHold(RuntimeError):
    """Conservative fail-closed Worker boundary, without retry authorization."""


async def run_issue_read_claim(
    *,
    connection_factory: ConnectionFactory,
    transport: IssueReadTransport,
    test_evidence_digest: str,
    event_id: int,
    job_id: int,
    worker_id: int,
    attempts: int,
    task_name: str = TASK_NAME,
) -> None:
    """One claimed Job: Claim -> Plan -> Intent -> GET/Verify -> Checkpoint.

    Any pre-existing Intent/Checkpoint blocks re-execution, even if the result
    is unknown. The external GET is read-only; ownership is revalidated by the
    Claim/Intent and Checkpoint writers, not held across the HTTP operation.
    """
    if any(type(x) is not int or x <= 0 for x in (event_id, job_id, worker_id)):
        raise IssueReadWorkerHold("invalid_job_context")
    if type(attempts) is not int or attempts < 0:
        raise IssueReadWorkerHold("invalid_job_context")
    if type(task_name) is not str or not task_name.strip():
        raise IssueReadWorkerHold("invalid_task_name")
    checked_digest(test_evidence_digest, "test_evidence_digest")

    async with await connection_factory() as connection:
        if not connection.autocommit:
            raise IssueReadWorkerHold("requires_autocommit_connection")
        target = await load_target(connection=connection, event_id=event_id)
        if target is None or target.kind != "issue":
            raise IssueReadWorkerHold("unsupported_target")

        running = await inspect_running_claim(
            connection=connection,
            event_id=event_id,
            job_id=job_id,
            expected_task_name=task_name,
            worker_id=worker_id,
            attempts=attempts,
        )
        if running.status != "observed" or running.started_event_id is None:
            raise IssueReadWorkerHold("claim_not_current")
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
        await seal_plan_structure(connection=connection, event_id=event_id, plan=_READ_PLAN)

        # Nothing in a prior attempt can be interpreted as proof of no GET.
        # In particular, an Intent with no Checkpoint is an unresolved result.
        if await load_operation_intent(connection=connection, event_id=event_id, operation_id=_READ.operation_id):
            raise IssueReadWorkerHold("intent_already_recorded")
        if await load_operation_checkpoint(connection=connection, event_id=event_id, operation_id=_READ.operation_id):
            raise IssueReadWorkerHold("checkpoint_already_recorded")

        intent = make_issue_read_intent(operation=_READ, target=target)
        bound = bind_issue_read_contract(
            intent=intent, target=target, transport=transport, test_evidence_digest=test_evidence_digest
        )
        if not ContractCatalog((bound,)).inspect(_READ, intent=intent).compatible:
            raise IssueReadWorkerHold("contract_incompatible")
        recorded = await record_operation_intent(
            connection=connection,
            event_id=event_id,
            generation=claim.generation,
            job_id=job_id,
            expected_task_name=task_name,
            worker_id=worker_id,
            attempts=attempts,
            started_event_id=started,
            expected_plan=_READ_PLAN,
            intent=intent,
        )
        if recorded.disposition != "recorded":
            raise IssueReadWorkerHold("intent_already_recorded")

        live = await inspect_claim_plan(
            connection=connection,
            event_id=event_id,
            generation=claim.generation,
            job_id=job_id,
            worker_id=worker_id,
            attempts=attempts,
            started_event_id=started,
            expected_task_name=task_name,
            expected_plan=_READ_PLAN,
        )
        if live.status != "observed":
            raise IssueReadWorkerHold("claim_not_current")

        if bound.execute is None or bound.decode is None or bound.verify is None or bound.concurrency.guard is None:
            raise IssueReadWorkerHold("contract_incomplete")
        await bound.concurrency.guard(intent)
        raw = await bound.execute(intent)
        if type(raw) is not bytes:
            raise IssueReadWorkerHold("read_result_invalid")
        checkpoint = bound.decode(raw)

        # A first GET is not a lease. If the Job was aborted or superseded
        # during that network call, do not issue the redundant verify GET.
        after_read = await inspect_claim_plan(
            connection=connection,
            event_id=event_id,
            generation=claim.generation,
            job_id=job_id,
            worker_id=worker_id,
            attempts=attempts,
            started_event_id=started,
            expected_task_name=task_name,
            expected_plan=_READ_PLAN,
        )
        if after_read.status != "observed":
            raise IssueReadWorkerHold("claim_not_current")

        if not await bound.verify(intent, checkpoint):
            raise IssueReadWorkerHold("verification_failed")
        saved = await record_operation_checkpoint(
            connection=connection,
            event_id=event_id,
            generation=claim.generation,
            job_id=job_id,
            expected_task_name=task_name,
            worker_id=worker_id,
            attempts=attempts,
            started_event_id=started,
            expected_plan=_READ_PLAN,
            checkpoint=checkpoint,
        )
        if saved.disposition != "recorded":
            raise IssueReadWorkerHold("checkpoint_already_recorded")


def register_issue_read_worker_task(
    *,
    app: App,
    connection_factory: ConnectionFactory,
    transport: IssueReadTransport,
    test_evidence_digest: str,
    queue: str = "default",
) -> Task[..., Any, Any]:
    """Explicitly register a non-retrying, GET-only Task on a caller's App.

    Registration alone never starts a Worker, connects to PostgreSQL, or
    executes a network call. The caller passes the returned Task to receiver.
    """
    if not callable(connection_factory) or not callable(transport):
        raise TypeError("connection_factory and transport must be callable")
    if type(queue) is not str or not queue.strip():
        raise ValueError("queue must be nonempty")
    checked_digest(test_evidence_digest, "test_evidence_digest")

    @app.task(name=TASK_NAME, queue=queue, pass_context=True, retry=False)
    async def _read_task(context: JobContext, event_id: int) -> None:
        job = context.job
        if job.id is None or job.worker_id is None:
            raise IssueReadWorkerHold("invalid_job_context")
        await run_issue_read_claim(
            connection_factory=connection_factory,
            transport=transport,
            test_evidence_digest=test_evidence_digest,
            event_id=event_id,
            job_id=job.id,
            worker_id=job.worker_id,
            attempts=job.attempts,
            task_name=TASK_NAME,
        )

    return _read_task
