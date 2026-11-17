"""Q79 append-only evidence that one remote write MAY have been attempted.

The record is committed BEFORE POST by a second DB session while the Issue
scope holds its original PostgreSQL target and Claim locks. It contains no
Webhook payload, API arguments, credential, or response. Recording does not
prove that GitHub received anything. A duplicate never authorizes a retry.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from ._canonical import checked_digest, checked_label, digest_bytes
from .immutable_intent import IntentIntegrityError, validate_intent_record
from .operation_intent import OperationIntent

ATTEMPT_VERSION: Literal["b3s-attempt:1"] = "b3s-attempt:1"
MAX_ATTEMPT_BYTES = 4096


class AttemptHold(RuntimeError):
    """Existing/invalid evidence or an unsafe local Attempt boundary."""


class AttemptIntegrityError(RuntimeError):
    """Persisted immutable Attempt evidence is corrupted or incompatible."""


@dataclass(frozen=True, slots=True)
class OperationAttempt:
    event_id: int
    operation_id: str
    origin_generation: int
    intent_digest: str
    version: Literal["b3s-attempt:1"] = ATTEMPT_VERSION

    def __post_init__(self) -> None:
        if type(self.event_id) is not int or self.event_id <= 0:
            raise ValueError("event_id must be positive.")
        if type(self.origin_generation) is not int or self.origin_generation <= 0:
            raise ValueError("origin_generation must be positive.")
        checked_label(self.operation_id, "operation_id")
        checked_digest(self.intent_digest, "intent_digest")
        if self.version != ATTEMPT_VERSION:
            raise ValueError("Unsupported Attempt version.")

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "version": self.version,
                "event_id": self.event_id,
                "operation_id": self.operation_id,
                "origin_generation": self.origin_generation,
                "intent_digest": self.intent_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def attempt_digest(self) -> str:
        return digest_bytes(self.canonical_bytes())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> OperationAttempt:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_ATTEMPT_BYTES:
            raise ValueError("Invalid Attempt evidence length.")
        try:
            data: object = json.loads(raw.decode("utf-8"))
            if type(data) is not dict:
                raise ValueError("Invalid Attempt structure.")
            checked = cast(dict[str, object], data)
            if set(checked) != {
                "version",
                "event_id",
                "operation_id",
                "origin_generation",
                "intent_digest",
            }:
                raise ValueError("Invalid Attempt field set.")
            item = cls(
                event_id=cast(int, checked["event_id"]),
                operation_id=cast(str, checked["operation_id"]),
                origin_generation=cast(int, checked["origin_generation"]),
                intent_digest=cast(str, checked["intent_digest"]),
                version=cast(Literal["b3s-attempt:1"], checked["version"]),
            )
        except (UnicodeError, TypeError, ValueError, KeyError, RecursionError) as error:
            raise ValueError("Invalid Attempt canonical representation.") from error
        if item.canonical_bytes() != raw:
            raise ValueError("Noncanonical Attempt bytes.")
        return item


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: OperationAttempt
    attempt_digest: str
    attempt_bytes: bytes


def validate_attempt_row(event_id: int, operation_id: str, row: tuple[Any, ...]) -> AttemptRecord:
    generation, intent_digest, version, digest, raw = row
    try:
        value = OperationAttempt.from_canonical_bytes(raw)
    except (ValueError, TypeError) as error:
        raise AttemptIntegrityError("Stored Attempt is not a supported canonical value.") from error
    if (
        value.event_id != event_id
        or value.operation_id != operation_id
        or value.origin_generation != generation
        or value.intent_digest != intent_digest
        or value.version != version
        or value.attempt_digest != digest
    ):
        raise AttemptIntegrityError("Persisted Attempt identity or digest is inconsistent.")
    return AttemptRecord(value, digest, raw)


_ATTEMPT_READ_COLUMNS = (
    "event_id",
    "operation_id",
    "origin_generation",
    "intent_digest",
    "attempt_version",
    "attempt_digest",
    "attempt_bytes",
)


async def has_attempt_store_shape(connection: AsyncConnection[tuple[Any, ...]]) -> bool:
    """Check minimum readable shape and the Q79 append-only constraints.

    This read-only catalog check does not certify column types, constraint/
    trigger implementation bodies, privileges, or future schema stability.
    """
    cursor = await connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS c
            WHERE c.oid = to_regclass('github_operation_attempts')
              AND c.relkind IN ('r', 'p')
              AND (
                  SELECT count(*) FROM pg_catalog.pg_attribute AS a
                  WHERE a.attrelid = c.oid AND a.attnum > 0
                    AND NOT a.attisdropped AND a.attname = ANY(%s)
              ) = %s
              AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_constraint AS k
                  WHERE k.conrelid = c.oid AND k.contype = 'p'
                    AND pg_catalog.pg_get_constraintdef(k.oid) =
                        'PRIMARY KEY (event_id, operation_id)'
              )
              AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_constraint AS k
                  WHERE k.conrelid = c.oid AND k.contype = 'f'
                    AND k.conname = 'github_attempt_intent_fk'
                    AND k.confrelid = to_regclass('github_operation_intents')
                    AND pg_catalog.pg_get_constraintdef(k.oid) =
                        'FOREIGN KEY (event_id, operation_id) REFERENCES ' ||
                        'github_operation_intents(event_id, operation_id)'
              )
              AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_constraint AS k
                  WHERE k.conrelid = c.oid AND k.contype = 'f'
                    AND k.conname = 'github_attempt_generation_fk'
                    AND k.confrelid = to_regclass('github_delivery_claim_generations')
                    AND pg_catalog.pg_get_constraintdef(k.oid) =
                        'FOREIGN KEY (event_id, origin_generation) REFERENCES ' ||
                        'github_delivery_claim_generations(event_id, generation)'
              )
              AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_trigger AS t
                  JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid
                  WHERE t.tgrelid = c.oid
                    AND t.tgname = 'github_operation_attempt_immutable'
                    AND NOT t.tgisinternal
                    AND t.tgenabled IN ('O', 'A')
                    AND t.tgtype = 27
                    AND p.proname = 'github_reject_operation_attempt_change'
                    AND p.pronamespace = c.relnamespace
              )
        )
        """,
        (list(_ATTEMPT_READ_COLUMNS), len(_ATTEMPT_READ_COLUMNS)),
    )
    return await cursor.fetchone() == (True,)


def _idle(connection: AsyncConnection[tuple[Any, ...]]) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise AttemptHold("Attempt store requires a dedicated idle autocommit connection.")


async def record_operation_attempt(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    origin_generation: int,
    intent: OperationIntent,
) -> AttemptRecord:
    """Commit once, AFTER the caller's target/Claim lock and BEFORE remote POST.

    The caller must already hold its target-scoped advisory and Claim locks on
    a DIFFERENT DB connection. This independent transaction cannot safely
    acquire those row locks again. The operation is intentionally not exposed
    as a Worker runner, nor as a remote execution authorization by itself.
    """
    _idle(connection)
    if type(intent) is not OperationIntent or (
        intent.operation.consumer_id,
        intent.operation.operation_type,
        intent.operation.contract_version,
    ) != ("github_issue", "add_label", "add_label:v2"):
        raise AttemptHold("Attempt requires the exact additive write v2 contract.")
    attempt = OperationAttempt(event_id, intent.operation.operation_id, origin_generation, intent.intent_digest)
    async with connection.transaction():
        cursor = await connection.execute(
            """
            SELECT created_generation, structure_digest, intent_version, intent_digest, intent_bytes
            FROM github_operation_intents WHERE event_id = %s AND operation_id = %s
            """,
            (event_id, intent.operation.operation_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise AttemptHold("The immutable Intent must be committed before an Attempt.")
        try:
            persisted = validate_intent_record(event_id, intent.operation.operation_id, row, "existing")
        except IntentIntegrityError as error:
            raise AttemptHold("Stored Intent is damaged.") from error
        if (
            persisted.created_generation != origin_generation
            or persisted.intent_digest != intent.intent_digest
            or persisted.intent_bytes != intent.canonical_bytes()
        ):
            raise AttemptHold("Attempt does not match its original Claim/Intent.")

        checkpoint = await connection.execute(
            "SELECT 1 FROM github_operation_checkpoints WHERE event_id = %s AND operation_id = %s",
            (event_id, intent.operation.operation_id),
        )
        if await checkpoint.fetchone() is not None:
            raise AttemptHold("An immutable Checkpoint already exists.")

        inserted = await connection.execute(
            """
            INSERT INTO github_operation_attempts
                (event_id, operation_id, origin_generation, intent_digest,
                 attempt_version, attempt_digest, attempt_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id, operation_id) DO NOTHING
            RETURNING origin_generation, intent_digest, attempt_version, attempt_digest, attempt_bytes
            """,
            (
                event_id,
                attempt.operation_id,
                origin_generation,
                attempt.intent_digest,
                ATTEMPT_VERSION,
                attempt.attempt_digest,
                attempt.canonical_bytes(),
            ),
        )
        result = await inserted.fetchone()
        if result is None:
            raise AttemptHold("An Attempt already exists; never automatically retry.")
        verified = validate_attempt_row(event_id, attempt.operation_id, result)
    return verified  # The top-level transaction has committed before returning.


async def load_operation_attempt(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    operation_id: str,
) -> AttemptRecord | None:
    """Read-only observation; never infers whether GitHub received a request."""
    _idle(connection)
    OperationAttempt(event_id, operation_id, 1, "sha256:" + "0" * 64)
    cur = await connection.execute(
        """
        SELECT origin_generation, intent_digest, attempt_version, attempt_digest, attempt_bytes
        FROM github_operation_attempts WHERE event_id = %s AND operation_id = %s
        """,
        (event_id, operation_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return validate_attempt_row(event_id, operation_id, row)
