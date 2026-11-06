"""Read-only evidence that the current Job claim and a sealed plan agree.

A coherent observation is not a lock, lease, execution permit, or proof that
the same claim remains active when a future Operation starts.
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from local_dev_mcp.tools.gh.webhook import supports_target_action

from .operation_plan import PlanStructure
from .schema import ENVELOPE_SCHEMA_VERSION
from .sealed_plan import PlanIntegrityError, validate_sealed_plan_row

ClaimPlanHoldReason = Literal[
    "receipt_missing",
    "unsupported_contract",
    "job_mismatch",
    "job_not_running",
    "claim_generation_mismatch",
    "plan_missing",
    "plan_invalid",
    "plan_mismatch",
]


@dataclass(frozen=True, slots=True)
class ClaimPlanObservation:
    status: Literal["observed", "hold"]
    reason: ClaimPlanHoldReason | None
    event_id: int
    generation: int
    structure_digest: str | None


def _hold(event_id: int, generation: int, reason: ClaimPlanHoldReason) -> ClaimPlanObservation:
    return ClaimPlanObservation("hold", reason, event_id, generation, None)


async def inspect_claim_plan(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    generation: int,
    job_id: int,
    worker_id: int,
    attempts: int,
    started_event_id: int,
    expected_task_name: str,
    expected_plan: PlanStructure,
) -> ClaimPlanObservation:
    """Observe current claim, latest generation, and immutable plan in one SELECT.

    No table changes, Job state transitions, Claim Generation writes, or remote
    calls occur. Any future Operation must revalidate its own execution
    preconditions transactionally; this snapshot grants no execution right.
    """
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Claim/plan inspection requires an idle autocommit connection.")
    if any(
        type(value) is not int or value <= 0 for value in (event_id, generation, job_id, worker_id, started_event_id)
    ):
        raise ValueError("Claim/plan identifiers must be positive integers.")
    if type(attempts) is not int or attempts < 0:
        raise ValueError("attempts must be a nonnegative integer.")
    if type(expected_task_name) is not str or not expected_task_name.strip():
        raise ValueError("expected_task_name must be a nonempty Task name.")
    if type(expected_plan) is not PlanStructure:
        raise TypeError("expected_plan must be a validated PlanStructure.")

    cursor = await connection.execute(
        """
        SELECT r.job_id, r.event_schema_version, r.envelope_schema_version,
               r.target_kind, r.target_number, r.action, r.event_type,
               r.repository, r.repository_id, r.target_updated_at, r.source_instance_id,
               j.task_name, j.args, j.status::text, j.worker_id, j.attempts,
               j.abort_requested, w.id,
               latest.id, latest.type::text,
               g.generation, g.job_id, g.started_event_id, g.worker_id, g.attempts,
               p.structure_version, p.structure_digest, p.structure_bytes
        FROM github_event_receipts AS r
        LEFT JOIN procrastinate_jobs AS j ON j.id = r.job_id
        LEFT JOIN procrastinate_workers AS w ON w.id = j.worker_id
        LEFT JOIN LATERAL (
            SELECT id, type FROM procrastinate_events
            WHERE job_id = j.id ORDER BY id DESC LIMIT 1
        ) AS latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT generation, job_id, started_event_id, worker_id, attempts
            FROM github_delivery_claim_generations
            WHERE event_id = r.event_id ORDER BY generation DESC LIMIT 1
        ) AS g ON TRUE
        LEFT JOIN github_sealed_plans AS p ON p.event_id = r.event_id
        WHERE r.event_id = %s
        """,
        (event_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return _hold(event_id, generation, "receipt_missing")
    (
        linked_job,
        event_version,
        envelope_version,
        kind,
        target_number,
        action,
        event_type,
        repository,
        repository_id,
        target_updated_at,
        source,
        task_name,
        args,
        status,
        job_worker,
        job_attempts,
        abort,
        registered_worker,
        latest_id,
        latest_type,
        current_generation,
        generation_job,
        generation_start,
        generation_worker,
        generation_attempts,
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
        or type(repository) is not str
        or not repository
        or type(repository_id) is not int
        or repository_id <= 0
        or type(target_number) is not int
        or target_number <= 0
        or type(target_updated_at) is not str
        or not target_updated_at
        or source != f"github:repository:{repository_id}"
    ):
        return _hold(event_id, generation, "unsupported_contract")
    if (
        linked_job != job_id
        or task_name != expected_task_name
        or type(args) is not dict
        or args != {"event_id": event_id}
        or type(cast(dict[str, object], args).get("event_id")) is not int
    ):
        return _hold(event_id, generation, "job_mismatch")
    if (
        status != "doing"
        or job_worker != worker_id
        or registered_worker != worker_id
        or type(job_attempts) is not int
        or job_attempts != attempts
        or abort is not False
        or latest_type != "started"
        or latest_id != started_event_id
    ):
        return _hold(event_id, generation, "job_not_running")
    if (
        type(current_generation) is not int
        or current_generation != generation
        or generation_job != job_id
        or generation_start != started_event_id
        or generation_worker != worker_id
        or generation_attempts != attempts
    ):
        return _hold(event_id, generation, "claim_generation_mismatch")
    if plan_version is None and plan_digest is None and plan_bytes is None:
        return _hold(event_id, generation, "plan_missing")
    try:
        sealed = validate_sealed_plan_row(event_id, (plan_version, plan_digest, plan_bytes), "existing")
    except PlanIntegrityError:
        return _hold(event_id, generation, "plan_invalid")
    if (
        sealed.structure_digest != expected_plan.structure_digest
        or sealed.structure_bytes != expected_plan.canonical_bytes()
    ):
        return _hold(event_id, generation, "plan_mismatch")
    return ClaimPlanObservation("observed", None, event_id, generation, sealed.structure_digest)
