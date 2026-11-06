"""Atomic GitHub event+normalized envelope receipt and Procrastinate queue insertion."""

from dataclasses import dataclass
from typing import Any, Literal

from procrastinate.tasks import Task
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from local_dev_mcp.tools.gh.webhook.verify import VerifiedDelivery, VerifiedTarget

from .schema import ENVELOPE_SCHEMA_VERSION


class EventConflictError(Exception):
    """A source-qualified delivery key was reused with different evidence."""


class ReceiptInvariantError(Exception):
    """A stored receipt has an invalid job or unsupported envelope contract."""


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    event_id: int
    job_id: int
    disposition: Literal["accepted", "duplicate"]


def _require_idle(connection: AsyncConnection[tuple[Any, ...]]) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Delivery requires an idle autocommit connection for a top-level commit.")


def _require_target(delivery: VerifiedDelivery) -> VerifiedTarget:
    """Reject evidence/target mismatches even if the internal model was forged."""
    if delivery.target is None:
        raise ValueError("A verified delivery with a supported target is required.")
    target = delivery.target
    observation = delivery.observation
    event = "issues" if target.kind == "issue" else "pull_request"
    if (
        observation.identity.source_instance_id != f"github:repository:{target.repository_id}"
        or observation.event_type != f"{event}.{target.action}"
        or observation.event_schema_version != "github-webhook:1"
        or not target.repository
        or not target.action
        or not target.updated_at
        or type(target.number) is not int
        or not 0 < target.number < 2**31
        or type(target.repository_id) is not int
        or target.repository_id <= 0
        or target.kind not in ("issue", "pull_request")
    ):
        raise ValueError("Verified event evidence and normalized target are inconsistent.")
    return target


async def _read_existing(
    connection: AsyncConnection[tuple[Any, ...]], delivery: VerifiedDelivery
) -> DeliveryReceipt | None:
    observation = delivery.observation
    target = _require_target(delivery)
    cur = await connection.execute(
        """
        SELECT event_id, job_id, event_type, event_schema_version, payload_digest,
               envelope_schema_version, repository, repository_id, target_kind,
               target_number, action, target_updated_at
        FROM github_event_receipts
        WHERE source_instance_id = %s AND event_key = %s
        """,
        (observation.identity.source_instance_id, observation.identity.event_key),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    (
        event_id,
        job_id,
        event_type,
        event_version,
        digest,
        envelope_version,
        repository,
        repository_id,
        kind,
        number,
        action,
        updated_at,
    ) = row
    if envelope_version != ENVELOPE_SCHEMA_VERSION:
        raise ReceiptInvariantError("Stored envelope contract version is not supported.")
    if (event_type, event_version, digest, repository, repository_id, kind, number, action, updated_at) != (
        observation.event_type,
        observation.event_schema_version,
        observation.payload_digest,
        target.repository,
        target.repository_id,
        target.kind,
        target.number,
        target.action,
        target.updated_at,
    ):
        raise EventConflictError("Source-qualified delivery identity was reused with conflicting evidence or target.")
    if not isinstance(job_id, int) or job_id <= 0:
        raise ReceiptInvariantError("Event receipt has no committed queued job.")
    return DeliveryReceipt(event_id=int(event_id), job_id=job_id, disposition="duplicate")


async def lookup_receipt(
    *, connection: AsyncConnection[tuple[Any, ...]], delivery: VerifiedDelivery
) -> DeliveryReceipt | None:
    """Read-only lookup after a lost ACK; absence never proves an in-flight commit failed."""
    _require_idle(connection)
    _require_target(delivery)
    return await _read_existing(connection, delivery)


async def accept_event(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    delivery: VerifiedDelivery,
    task: Task[..., Any, Any],
) -> DeliveryReceipt:
    """Commit receipt, normalized envelope and job atomically; never execute a task.

    The caller must supply a verified, supported GitHub delivery and an idle
    autocommit connection, using a Procrastinate task that accepts only event_id.
    The returned success means the top-level transaction committed, not that an
    HTTP response reached GitHub or any remote mutation happened.
    """
    _require_idle(connection)
    target = _require_target(delivery)
    obs = delivery.observation

    # PostgreSQL's unique constraint serializes concurrent delivery identities.
    async with connection.transaction():
        cur = await connection.execute(
            """
            INSERT INTO github_event_receipts (
                source_instance_id, event_key, event_type, event_schema_version,
                payload_digest, observed_at, envelope_schema_version,
                repository, repository_id, target_kind, target_number,
                action, target_updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_instance_id, event_key) DO NOTHING
            RETURNING event_id
            """,
            (
                obs.identity.source_instance_id,
                obs.identity.event_key,
                obs.event_type,
                obs.event_schema_version,
                obs.payload_digest,
                obs.observed_at,
                ENVELOPE_SCHEMA_VERSION,
                target.repository,
                target.repository_id,
                target.kind,
                target.number,
                target.action,
                target.updated_at,
            ),
        )
        inserted = await cur.fetchone()
        if inserted is None:
            existing = await _read_existing(connection, delivery)
            if existing is None:
                raise ReceiptInvariantError("Unique event identity has no committed receipt.")
            return existing

        event_id = int(inserted[0])
        job_id = await task.configure(connection=connection).defer_async(event_id=event_id)
        cur = await connection.execute(
            "UPDATE github_event_receipts SET job_id = %s WHERE event_id = %s RETURNING job_id",
            (job_id, event_id),
        )
        linked = await cur.fetchone()
        if linked is None or linked[0] != job_id:
            raise ReceiptInvariantError("New event receipt could not be linked to the job.")
    return DeliveryReceipt(event_id=event_id, job_id=job_id, disposition="accepted")


async def load_target(*, connection: AsyncConnection[tuple[Any, ...]], event_id: int) -> VerifiedTarget | None:
    """Read the committed minimal Envelope by Job event ID, never an Intent.

    Loading a target does not authorize GitHub mutations, requeueing or retrying.
    Unsupported/corrupt records must be held rather than guessed.
    """
    _require_idle(connection)
    if type(event_id) is not int or event_id <= 0:
        raise ValueError("event_id must be a positive integer.")
    cur = await connection.execute(
        """
        SELECT envelope_schema_version, repository, repository_id,
               target_kind, target_number, action, target_updated_at, job_id
        FROM github_event_receipts WHERE event_id = %s
        """,
        (event_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    version, repository, repository_id, kind, number, action, updated_at, job_id = row
    if (
        version != ENVELOPE_SCHEMA_VERSION
        or kind not in ("issue", "pull_request")
        or not isinstance(repository, str)
        or not repository
        or type(repository_id) is not int
        or repository_id <= 0
        or type(number) is not int
        or number <= 0
        or not isinstance(action, str)
        or not action
        or not isinstance(updated_at, str)
        or not updated_at
        or type(job_id) is not int
        or job_id <= 0
    ):
        raise ReceiptInvariantError("Event receipt has an unsupported or incomplete normalized target.")
    return VerifiedTarget(
        kind=kind,
        repository=repository,
        repository_id=repository_id,
        number=number,
        action=action,
        updated_at=updated_at,
    )
