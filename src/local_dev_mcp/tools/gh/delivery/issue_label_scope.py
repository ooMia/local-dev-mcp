"""Q78=A: PostgreSQL target-scoped serialization for participating Workers.

The transaction-scoped advisory lock protects read→additive POST→read only
while the dedicated DB session remains alive. It does NOT fence GitHub users,
other apps, a disconnected worker, or an earlier uncertain remote POST.
No claim acquisition, DDL, checkpoint creation or retry is done here.
"""

import hashlib
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget

from .claim_guard import ClaimGuardHold, require_current_claim_and_sealed_plan
from .immutable_intent import IntentIntegrityError, validate_intent_record
from .issue_label_consumer import AttemptPermit, IssueLabelHold
from .issue_read_consumer import issue_read_input_digest
from .lineage_store import LineageHold, LineageIntegrityError, require_source_checkpoints, validate_lineage_row
from .operation_attempt import (
    AttemptHold,
    AttemptIntegrityError,
    has_attempt_store_shape,
    record_operation_attempt,
    validate_attempt_row,
)
from .operation_intent import OperationIntent
from .operation_lineage import OperationLineage
from .operation_plan import PlanStructure


def issue_label_lock_key(target: VerifiedTarget) -> int:
    """Stable signed key: namespace plus numeric repository/Issue identity.

    SHA-256 truncation can over-serialize on collision, not authorize a write.
    Every participating Worker must use the same key for the same Issue.
    """
    issue_read_input_digest(target)
    raw = f"local-dev-mcp:issue-label-scope:v1:{target.repository_id}:{target.number}".encode("ascii")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


@dataclass(slots=True, repr=False)
class _PostgresAttemptPermit:
    connection: AsyncConnection[tuple[Any, ...]]
    event_id: int
    generation: int
    intent: OperationIntent
    armed: bool = False

    async def commit_attempt(self) -> None:
        if self.armed:
            raise IssueLabelHold("write_scope_attempt_already_armed")
        self.armed = True  # A failed/unknown Commit cannot be attempted again.
        try:
            await record_operation_attempt(
                connection=self.connection,
                event_id=self.event_id,
                origin_generation=self.generation,
                intent=self.intent,
            )
        except (AttemptHold, AttemptIntegrityError, psycopg.Error) as error:
            raise IssueLabelHold("write_scope_attempt_commit_failed") from error


@dataclass(frozen=True, slots=True, repr=False)
class PostgresIssueLabelWriteScope:
    """Dedicated autocommit connection and an existing active Worker Claim."""

    connection: AsyncConnection[tuple[Any, ...]]
    attempt_connection: AsyncConnection[tuple[Any, ...]]
    target: VerifiedTarget
    event_id: int
    generation: int
    job_id: int
    expected_task_name: str
    worker_id: int
    attempts: int
    started_event_id: int
    expected_plan: PlanStructure

    def __post_init__(self) -> None:
        issue_label_lock_key(self.target)
        if self.attempt_connection is self.connection:
            raise ValueError("The pre-POST Attempt requires a separate transaction connection.")
        for name in ("event_id", "generation", "job_id", "worker_id", "started_event_id"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("attempts must be a nonnegative integer.")
        if type(self.expected_task_name) is not str or not self.expected_task_name.strip():
            raise ValueError("expected_task_name is required.")
        if type(self.expected_plan) is not PlanStructure:
            raise TypeError("Expected an immutable sealed PlanStructure.")

    def __call__(self, intent: OperationIntent) -> AbstractAsyncContextManager[AttemptPermit]:
        return self._hold(intent)

    @asynccontextmanager
    async def _hold(self, intent: OperationIntent) -> AsyncGenerator[AttemptPermit]:
        conn = self.connection
        if (
            type(intent) is not OperationIntent
            or intent.operation.consumer_id != "github_issue"
            or intent.operation.operation_type != "add_label"
            or intent.operation.contract_version != "add_label:v2"
            or intent.operation not in self.expected_plan.operations
        ):
            raise IssueLabelHold("write_scope_wrong_operation")
        if (
            not conn.autocommit
            or conn.info.transaction_status != TransactionStatus.IDLE
            or not self.attempt_connection.autocommit
            or self.attempt_connection.info.transaction_status != TransactionStatus.IDLE
        ):
            raise IssueLabelHold("write_scope_requires_dedicated_idle_autocommit_connections")

        async with conn.transaction():
            # The Q78 lock and the independently committed Q79 Attempt must
            # refer to the same unqualified PostgreSQL relations. Comparing
            # names alone is unsafe when search_path selects different tables.
            identity_sql = """
                SELECT current_database(), current_schema(),
                       to_regclass('github_event_receipts')::oid,
                       to_regclass('github_operation_intents')::oid,
                       to_regclass('github_operation_checkpoints')::oid,
                       to_regclass('github_delivery_claim_generations')::oid,
                       to_regclass('github_operation_attempts')::oid,
                       pg_backend_pid()
            """
            first_cursor = await conn.execute(identity_sql)
            first = await first_cursor.fetchone()
            second_cursor = await self.attempt_connection.execute(identity_sql)
            second = await second_cursor.fetchone()
            if (
                first is None
                or second is None
                or first[:-1] != second[:-1]
                or first[-1] == second[-1]
                or any(value is None for value in first[2:6])
            ):
                raise IssueLabelHold("write_scope_attempt_connection_mismatch")

            lock = await conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (issue_label_lock_key(self.target),))
            if await lock.fetchone() != (True,):
                raise IssueLabelHold("write_scope_issue_busy")

            try:
                await require_current_claim_and_sealed_plan(
                    connection=conn,
                    event_id=self.event_id,
                    generation=self.generation,
                    job_id=self.job_id,
                    expected_task_name=self.expected_task_name,
                    worker_id=self.worker_id,
                    attempts=self.attempts,
                    started_event_id=self.started_event_id,
                    expected_plan=self.expected_plan,
                )
            except ClaimGuardHold as error:
                raise IssueLabelHold("write_scope_claim_or_plan_invalid") from error

            bound = await conn.execute(
                """
                SELECT target_kind, repository, repository_id, target_number, action, target_updated_at
                FROM github_event_receipts WHERE event_id = %s
                """,
                (self.event_id,),
            )
            if await bound.fetchone() != (
                self.target.kind,
                self.target.repository,
                self.target.repository_id,
                self.target.number,
                self.target.action,
                self.target.updated_at,
            ):
                raise IssueLabelHold("write_scope_receipt_target_mismatch")

            current = await conn.execute(
                """
                SELECT created_generation, structure_digest, intent_version, intent_digest, intent_bytes
                FROM github_operation_intents WHERE event_id = %s AND operation_id = %s
                """,
                (self.event_id, intent.operation.operation_id),
            )
            current_row = await current.fetchone()
            if current_row is None:
                raise IssueLabelHold("write_scope_intent_not_committed")
            try:
                record = validate_intent_record(self.event_id, intent.operation.operation_id, current_row, "existing")
            except IntentIntegrityError as error:
                raise IssueLabelHold("write_scope_intent_corrupted") from error
            if (
                record.created_generation != self.generation
                or record.structure_digest != self.expected_plan.structure_digest
                or record.intent_digest != intent.intent_digest
                or record.intent_bytes != intent.canonical_bytes()
            ):
                raise IssueLabelHold("write_scope_intent_mismatch")

            checkpoint = await conn.execute(
                """
                SELECT 1 FROM github_operation_checkpoints
                WHERE event_id = %s AND operation_id = %s
                """,
                (self.event_id, intent.operation.operation_id),
            )
            if await checkpoint.fetchone() is not None:
                raise IssueLabelHold("write_scope_checkpoint_already_recorded")

            lineage = await conn.execute(
                """
                SELECT lineage_version, lineage_digest, lineage_bytes
                FROM github_operation_lineages WHERE event_id = %s AND operation_id = %s
                """,
                (self.event_id, intent.operation.operation_id),
            )
            raw_lineage = await lineage.fetchone()
            if not intent.operation.depends_on or raw_lineage is None:
                raise IssueLabelHold("write_scope_missing_dependency_lineage")
            try:
                lineage_record = validate_lineage_row(
                    self.event_id, intent.operation.operation_id, raw_lineage, "existing"
                )
                linked = OperationLineage.from_canonical_bytes(lineage_record.lineage_bytes)
                await require_source_checkpoints(
                    connection=conn,
                    event_id=self.event_id,
                    expected_plan=self.expected_plan,
                    intent=intent,
                    lineage=linked,
                )
            except (LineageHold, LineageIntegrityError, ValueError, TypeError) as error:
                raise IssueLabelHold("write_scope_dependency_invalid") from error

            if not await has_attempt_store_shape(conn):
                raise IssueLabelHold("write_scope_attempt_store_unavailable")
            prior = await conn.execute(
                """
                SELECT origin_generation, intent_digest, attempt_version, attempt_digest, attempt_bytes
                FROM github_operation_attempts WHERE event_id = %s AND operation_id = %s
                """,
                (self.event_id, intent.operation.operation_id),
            )
            previous_attempt = await prior.fetchone()
            if previous_attempt is not None:
                try:
                    validate_attempt_row(self.event_id, intent.operation.operation_id, previous_attempt)
                except AttemptIntegrityError as error:
                    raise IssueLabelHold("write_scope_attempt_corrupted") from error
                raise IssueLabelHold("write_scope_attempt_already_recorded")

            # The Claim row locks and target advisory xact lock stay active
            # while a SECOND connection commits the Attempt. No POST is
            # permitted before the permit returns from its top-level Commit.
            yield _PostgresAttemptPermit(self.attempt_connection, self.event_id, self.generation, intent)
            alive = await conn.execute("SELECT 1")
            if await alive.fetchone() != (1,):
                raise IssueLabelHold("write_scope_connection_unverified")
