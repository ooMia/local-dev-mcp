"""Shared transactional Claim/Plan revalidation for evidence writers.

Call only inside a top-level transaction before writing an immutable record.
Receipt then Procrastinate Job locking matches Claim Generation lock order.
This local guard is NOT a GitHub API fencing or mutation authorization.
"""

from typing import Any, cast

from psycopg import AsyncConnection

from local_dev_mcp.tools.gh.webhook import supports_target_action

from .operation_plan import PlanStructure
from .schema import ENVELOPE_SCHEMA_VERSION
from .sealed_plan import PlanIntegrityError, validate_sealed_plan_row


class ClaimGuardHold(RuntimeError):
    """Current Job, Generation, Envelope or Plan does not match."""


async def require_current_claim_and_sealed_plan(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    generation: int,
    job_id: int,
    expected_task_name: str,
    worker_id: int,
    attempts: int,
    started_event_id: int,
    expected_plan: PlanStructure,
) -> None:
    """Revalidate while the caller's transaction holds Receipt/Job locks.

    A successful check authorizes only local evidence persistence in that
    transaction. It cannot make an external mutation or retry safe.
    """
    structure_digest = expected_plan.structure_digest
    receipt = await connection.execute(
        """
        SELECT job_id, event_schema_version, envelope_schema_version,
               target_kind, action, event_type, repository_id, source_instance_id,
               repository, target_number, target_updated_at
        FROM github_event_receipts WHERE event_id = %s FOR UPDATE
        """,
        (event_id,),
    )
    row = await receipt.fetchone()
    if row is None:
        raise ClaimGuardHold("Receipt does not exist.")
    (
        stored_job,
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
    ) = row
    if (
        stored_job != job_id
        or event_version != "github-webhook:1"
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
        raise ClaimGuardHold("Receipt or Envelope contract is unsupported.")

    job = await connection.execute(
        """
        SELECT j.task_name, j.args, j.status::text, j.worker_id, j.attempts,
               j.abort_requested, w.id
        FROM procrastinate_jobs AS j
        LEFT JOIN procrastinate_workers AS w ON w.id = j.worker_id
        WHERE j.id = %s FOR UPDATE OF j
        """,
        (job_id,),
    )
    job_row = await job.fetchone()
    if job_row is None:
        raise ClaimGuardHold("Job no longer exists.")
    task_name, args, status, stored_worker, stored_attempts, abort, registered_worker = job_row
    if (
        task_name != expected_task_name
        or type(args) is not dict
        or args != {"event_id": event_id}
        or type(cast(dict[str, object], args).get("event_id")) is not int
        or status != "doing"
        or stored_worker != worker_id
        or registered_worker != worker_id
        or type(stored_attempts) is not int
        or stored_attempts != attempts
        or abort is not False
    ):
        raise ClaimGuardHold("Job is not owned by the expected active worker.")

    latest = await connection.execute(
        "SELECT id, type::text FROM procrastinate_events WHERE job_id = %s ORDER BY id DESC LIMIT 1",
        (job_id,),
    )
    if await latest.fetchone() != (started_event_id, "started"):
        raise ClaimGuardHold("Observed Job start has been superseded.")

    claim = await connection.execute(
        """
        SELECT generation, job_id, started_event_id, worker_id, attempts
        FROM github_delivery_claim_generations
        WHERE event_id = %s ORDER BY generation DESC LIMIT 1
        """,
        (event_id,),
    )
    if await claim.fetchone() != (generation, job_id, started_event_id, worker_id, attempts):
        raise ClaimGuardHold("Claim Generation is missing or stale.")

    sealed = await connection.execute(
        """
        SELECT structure_version, structure_digest, structure_bytes
        FROM github_sealed_plans WHERE event_id = %s
        """,
        (event_id,),
    )
    sealed_row = await sealed.fetchone()
    if sealed_row is None:
        raise ClaimGuardHold("Plan must be durably sealed before Intent.")
    try:
        plan = validate_sealed_plan_row(event_id, sealed_row, "existing")
    except PlanIntegrityError as error:
        raise ClaimGuardHold("Sealed Plan is unsupported or corrupted.") from error
    if plan.structure_digest != structure_digest or plan.structure_bytes != expected_plan.canonical_bytes():
        raise ClaimGuardHold("Expected Plan differs from the sealed Plan.")
