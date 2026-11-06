"""Read-only Procrastinate running-claim evidence (not a Delivery claim).

A current PostgreSQL row and started-event reference are observations, not
ownership leases, persisted claim generations, or authority to mutate GitHub.
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

ClaimHoldReason = Literal[
    "receipt_missing",
    "job_mismatch",
    "job_not_running",
    "worker_mismatch",
    "attempt_mismatch",
    "abort_requested",
    "start_event_missing",
]


@dataclass(frozen=True, slots=True)
class RunningClaimObservation:
    status: Literal["observed", "hold"]
    reason: ClaimHoldReason | None
    event_id: int
    job_id: int
    worker_id: int
    attempts: int
    started_event_id: int | None


def _hold(
    *, event_id: int, job_id: int, worker_id: int, attempts: int, reason: ClaimHoldReason
) -> RunningClaimObservation:
    return RunningClaimObservation("hold", reason, event_id, job_id, worker_id, attempts, None)


async def inspect_running_claim(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    job_id: int,
    expected_task_name: str,
    worker_id: int,
    attempts: int,
) -> RunningClaimObservation:
    """Read matching job, worker and its latest lifecycle event in one snapshot.

    The caller supplies its Procrastinate JobContext values. Status and worker
    identifiers can change immediately after the query; even 'observed' grants
    no right to seal a plan, issue a Delivery Claim Generation or execute work.
    """
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Inspection requires an idle autocommit connection.")
    if any(type(value) is not int or value <= 0 for value in (event_id, job_id, worker_id)):
        raise ValueError("event_id, job_id, worker_id must be positive integers.")
    if type(attempts) is not int or attempts < 0:
        raise ValueError("attempts must be a nonnegative integer.")
    if type(expected_task_name) is not str or not expected_task_name.strip():
        raise ValueError("expected_task_name must be a nonempty task name.")

    cursor = await connection.execute(
        """
        SELECT r.job_id, j.id, j.task_name, j.args, j.status::text,
               j.worker_id, j.attempts, j.abort_requested, w.id, latest.id, latest.type::text
        FROM github_event_receipts AS r
        LEFT JOIN procrastinate_jobs AS j ON j.id = r.job_id
        LEFT JOIN procrastinate_workers AS w ON w.id = j.worker_id
        LEFT JOIN LATERAL (
            SELECT e.id, e.type
            FROM procrastinate_events AS e
            WHERE e.job_id = j.id
            ORDER BY e.id DESC LIMIT 1
        ) AS latest ON TRUE
        WHERE r.event_id = %s
        """,
        (event_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return _hold(event_id=event_id, job_id=job_id, worker_id=worker_id, attempts=attempts, reason="receipt_missing")
    (
        linked_job_id,
        stored_job_id,
        task_name,
        args,
        status,
        stored_worker,
        stored_attempts,
        abort,
        worker,
        started_id,
        last_event_type,
    ) = row
    if (
        linked_job_id != job_id
        or stored_job_id != job_id
        or task_name != expected_task_name
        or type(args) is not dict
        or args != {"event_id": event_id}
        or type(cast(dict[str, object], args).get("event_id")) is not int
    ):
        reason: ClaimHoldReason = "job_mismatch"
    elif status != "doing":
        reason = "job_not_running"
    elif stored_worker != worker_id or worker != worker_id:
        reason = "worker_mismatch"
    elif type(stored_attempts) is not int or stored_attempts != attempts:
        reason = "attempt_mismatch"
    elif abort is not False:
        reason = "abort_requested"
    elif last_event_type != "started" or type(started_id) is not int or started_id <= 0:
        reason = "start_event_missing"
    else:
        return RunningClaimObservation("observed", None, event_id, job_id, worker_id, attempts, started_id)
    return _hold(event_id=event_id, job_id=job_id, worker_id=worker_id, attempts=attempts, reason=reason)
