"""Append-only local Delivery Claim Generation bookkeeping.

Procrastinate owns the real Job claim. This module records independently
versioned evidence only; it does not create an execution lease, fence an
external GitHub API mutation, claim/retry a Job, or grant authority to act.
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from local_dev_mcp.tools.gh.webhook import supports_target_action

from .schema import ENVELOPE_SCHEMA_VERSION


class ClaimGenerationHold(RuntimeError):
    """Observed Procrastinate execution is stale, inconsistent, or unsupported."""


class ClaimGenerationIntegrityError(RuntimeError):
    """Persisted claim metadata cannot be reconciled."""


@dataclass(frozen=True, slots=True)
class ClaimGenerationRecord:
    event_id: int
    generation: int
    job_id: int
    started_event_id: int
    worker_id: int
    attempts: int
    disposition: Literal["recorded", "existing"]


def _require_idle(connection: AsyncConnection[tuple[Any, ...]]) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Claim generation requires an idle autocommit connection.")


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def _checked_row(
    event_id: int, row: tuple[Any, ...], disposition: Literal["recorded", "existing"]
) -> ClaimGenerationRecord:
    generation, job_id, started_id, worker_id, attempts = row
    if (
        type(generation) is not int
        or generation <= 0
        or type(job_id) is not int
        or job_id <= 0
        or type(started_id) is not int
        or started_id <= 0
        or type(worker_id) is not int
        or worker_id <= 0
        or type(attempts) is not int
        or attempts < 0
    ):
        raise ClaimGenerationIntegrityError("Invalid stored Delivery Claim Generation.")
    return ClaimGenerationRecord(event_id, generation, job_id, started_id, worker_id, attempts, disposition)


async def record_claim_generation(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    job_id: int,
    expected_task_name: str,
    worker_id: int,
    attempts: int,
    started_event_id: int,
) -> ClaimGenerationRecord:
    """Persist one new Claim Generation only for a currently running Job.

    The Receipt lock serializes competing generation writers. Rechecks the
    Procrastinate Job under a row lock, including *the exact* started event
    observed by the caller. A repeated current start is idempotent; a stale
    start never returns an active Generation or increments the counter.
    """
    _require_idle(connection)
    for name, value in (
        ("event_id", event_id),
        ("job_id", job_id),
        ("worker_id", worker_id),
        ("started_event_id", started_event_id),
    ):
        _positive(value, name)
    if type(attempts) is not int or attempts < 0:
        raise ValueError("attempts must be a nonnegative integer.")
    if type(expected_task_name) is not str or not expected_task_name.strip():
        raise ValueError("expected_task_name must be a nonempty registered Task name.")

    async with connection.transaction():
        receipt = await connection.execute(
            """
            SELECT job_id, event_schema_version, envelope_schema_version,
                   target_kind, action, event_type, repository_id, source_instance_id
            FROM github_event_receipts WHERE event_id = %s FOR UPDATE
            """,
            (event_id,),
        )
        receipt_row = await receipt.fetchone()
        if receipt_row is None:
            raise ClaimGenerationHold("Receipt does not exist.")
        stored_job, event_version, envelope_version, kind, action, event_type, repository_id, source = receipt_row
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
        ):
            raise ClaimGenerationHold("Receipt or target contract is not supported.")

        job = await connection.execute(
            """
            SELECT j.task_name, j.args, j.status::text, j.worker_id,
                   j.attempts, j.abort_requested, w.id
            FROM procrastinate_jobs AS j
            LEFT JOIN procrastinate_workers AS w ON w.id = j.worker_id
            WHERE j.id = %s
            FOR UPDATE OF j
            """,
            (job_id,),
        )
        job_row = await job.fetchone()
        if job_row is None:
            raise ClaimGenerationHold("Procrastinate Job no longer exists.")
        task_name, args, status, current_worker, current_attempts, abort, registered_worker = job_row
        if (
            task_name != expected_task_name
            or type(args) is not dict
            or args != {"event_id": event_id}
            or type(cast(dict[str, object], args).get("event_id")) is not int
            or status != "doing"
            or current_worker != worker_id
            or registered_worker != worker_id
            or type(current_attempts) is not int
            or current_attempts != attempts
            or abort is not False
        ):
            raise ClaimGenerationHold("Procrastinate Job ownership is not current.")

        last_event = await connection.execute(
            """
            SELECT id, type::text
            FROM procrastinate_events
            WHERE job_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        )
        last_row = await last_event.fetchone()
        if last_row != (started_event_id, "started"):
            raise ClaimGenerationHold("The observed start event was superseded or removed.")

        previous = await connection.execute(
            """
            SELECT generation, job_id, started_event_id, worker_id, attempts
            FROM github_delivery_claim_generations
            WHERE event_id = %s ORDER BY generation DESC LIMIT 1
            """,
            (event_id,),
        )
        prior_row = await previous.fetchone()
        if prior_row is None:
            generation = 1
        else:
            prior = _checked_row(event_id, prior_row, "existing")
            if (
                prior.job_id != job_id
                or started_event_id < prior.started_event_id
                or (
                    started_event_id == prior.started_event_id
                    and (prior.worker_id != worker_id or prior.attempts != attempts)
                )
            ):
                raise ClaimGenerationHold("The running execution conflicts with recorded ownership.")
            if started_event_id == prior.started_event_id:
                return prior
            generation = prior.generation + 1

        inserted = await connection.execute(
            """
            INSERT INTO github_delivery_claim_generations
                (event_id, generation, job_id, started_event_id, worker_id, attempts)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING generation, job_id, started_event_id, worker_id, attempts
            """,
            (event_id, generation, job_id, started_event_id, worker_id, attempts),
        )
        row = await inserted.fetchone()
        if row is None:
            raise ClaimGenerationIntegrityError("Could not read committed generation metadata.")
        result = _checked_row(event_id, row, "recorded")
    return result


async def load_latest_claim_generation(
    *, connection: AsyncConnection[tuple[Any, ...]], event_id: int
) -> ClaimGenerationRecord | None:
    """Read-only latest generation lookup; never an execution authorization."""
    _require_idle(connection)
    _positive(event_id, "event_id")
    cur = await connection.execute(
        """
        SELECT generation, job_id, started_event_id, worker_id, attempts
        FROM github_delivery_claim_generations
        WHERE event_id = %s ORDER BY generation DESC LIMIT 1
        """,
        (event_id,),
    )
    row = await cur.fetchone()
    return None if row is None else _checked_row(event_id, row, "existing")
