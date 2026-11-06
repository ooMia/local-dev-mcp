"""Read-only pre-claim reconciliation of a GitHub receipt and its queued job.

This observation is NOT permission to claim, execute, retry or mutate GitHub.
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from local_dev_mcp.tools.gh.webhook import supports_target_action

from .schema import ENVELOPE_SCHEMA_VERSION

ProbeReason = Literal["receipt_missing", "unsupported_contract", "job_unlinked", "job_mismatch"]
ProbeStatus = Literal["observed", "hold"]


@dataclass(frozen=True, slots=True)
class QueuedEnvelopeProbe:
    """An immutable, deliberately non-authorizing observation."""

    status: ProbeStatus
    reason: ProbeReason | None
    event_id: int
    job_id: int


def _hold(event_id: int, job_id: int, reason: ProbeReason) -> QueuedEnvelopeProbe:
    return QueuedEnvelopeProbe(status="hold", reason=reason, event_id=event_id, job_id=job_id)


async def inspect_queued_envelope(
    *, connection: AsyncConnection[tuple[Any, ...]], event_id: int, job_id: int, expected_task_name: str
) -> QueuedEnvelopeProbe:
    """Inspect both records with one SELECT, without claiming or requeuing.

    A matching pair is only *observed*: a subsequent Claim, fresh state check,
    immutable Intent, sealed plan and operation-specific safety proof are still
    required before any GitHub side effect. Missing/unsupported data is held.
    """
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Queue inspection requires an idle autocommit connection.")
    if type(event_id) is not int or event_id <= 0 or type(job_id) is not int or job_id <= 0:
        raise ValueError("event_id and job_id must be positive integers.")
    if type(expected_task_name) is not str or not expected_task_name.strip():
        raise ValueError("expected_task_name must be a nonempty registered task name.")

    cur = await connection.execute(
        """
        SELECT r.source_instance_id, r.event_type, r.event_schema_version,
               r.payload_digest, r.envelope_schema_version, r.repository,
               r.repository_id, r.target_kind, r.target_number, r.action,
               r.target_updated_at, r.job_id, j.id, j.args, j.task_name
        FROM github_event_receipts AS r
        LEFT JOIN procrastinate_jobs AS j ON j.id = r.job_id
        WHERE r.event_id = %s
        """,
        (event_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return _hold(event_id, job_id, "receipt_missing")
    (
        source,
        event_type,
        event_version,
        digest,
        envelope_version,
        repository,
        repo_id,
        kind,
        number,
        action,
        updated,
        stored_job_id,
        queued_job_id,
        job_args,
        queued_task_name,
    ) = row
    if (
        event_version != "github-webhook:1"
        or envelope_version != ENVELOPE_SCHEMA_VERSION
        or kind not in ("issue", "pull_request")
        or type(repo_id) is not int
        or repo_id <= 0
        or source != f"github:repository:{repo_id}"
        or type(number) is not int
        or not 0 < number < 2**31
        or not isinstance(repository, str)
        or not repository
        or not isinstance(action, str)
        or not action
        or not supports_target_action(kind, action)
        or not isinstance(updated, str)
        or not updated
        or event_type != f"{'issues' if kind == 'issue' else 'pull_request'}.{action}"
        or not isinstance(digest, str)
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in digest[7:])
    ):
        return _hold(event_id, job_id, "unsupported_contract")
    if type(stored_job_id) is not int or stored_job_id <= 0:
        return _hold(event_id, job_id, "job_unlinked")
    if (
        stored_job_id != job_id
        or queued_job_id != job_id
        or queued_task_name != expected_task_name
        or type(job_args) is not dict
        or job_args != {"event_id": event_id}
        or type(cast(dict[str, object], job_args).get("event_id")) is not int
    ):
        return _hold(event_id, job_id, "job_mismatch")

    return QueuedEnvelopeProbe(status="observed", reason=None, event_id=event_id, job_id=job_id)
