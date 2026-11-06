"""Append-only PostgreSQL Checkpoint evidence for one Operation (Q71).

The caller presents verification evidence, but the store neither performs
verification nor authorizes GitHub effects. Uncertain remote outcomes must
not be converted into successful Checkpoints by this module.
"""

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from .claim_guard import ClaimGuardHold, require_current_claim_and_sealed_plan
from .immutable_intent import IntentIntegrityError, load_operation_intent, validate_intent_record
from .operation_checkpoint import CHECKPOINT_VERSION, OperationCheckpoint
from .operation_intent import OperationIntent
from .operation_lineage import OperationLineage
from .operation_plan import PlanStructure
from .sealed_plan import validate_sealed_plan_row


class CheckpointHold(RuntimeError):
    """Missing or stale Claim/Intent/Plan evidence prevents local persistence."""


class CheckpointConflictError(RuntimeError):
    """Existing immutable Checkpoint is different from the submitted evidence."""


class CheckpointIntegrityError(RuntimeError):
    """Stored Checkpoint evidence is damaged or unsupported."""


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    event_id: int
    operation_id: str
    recorded_generation: int
    intent_digest: str
    checkpoint_digest: str
    checkpoint_bytes: bytes
    disposition: Literal["recorded", "existing"]


def validate_checkpoint_row(
    event_id: int, operation_id: str, row: tuple[Any, ...], disposition: Literal["recorded", "existing"]
) -> CheckpointRecord:
    generation, intent_digest, version, digest, raw = row
    if (
        type(generation) is not int
        or generation <= 0
        or type(intent_digest) is not str
        or len(intent_digest) != 71
        or version != CHECKPOINT_VERSION
        or type(raw) is not bytes
        or type(digest) is not str
        or digest != "sha256:" + hashlib.sha256(raw).hexdigest()
    ):
        raise CheckpointIntegrityError("Corrupted or unsupported Checkpoint evidence.")
    try:
        checked = OperationCheckpoint.from_canonical_bytes(raw)
    except (ValueError, TypeError) as error:
        raise CheckpointIntegrityError("Noncanonical or unsupported Checkpoint.") from error
    if checked.operation_id != operation_id or checked.intent_digest != intent_digest:
        raise CheckpointIntegrityError("Checkpoint does not match its stored identity/Intent.")
    return CheckpointRecord(event_id, operation_id, generation, intent_digest, digest, raw, disposition)


def _require_idle(connection: AsyncConnection[tuple[Any, ...]]) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Checkpoint persistence requires an idle autocommit connection.")


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


async def record_operation_checkpoint(
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
    checkpoint: OperationCheckpoint,
) -> CheckpointRecord:
    """Record one Checkpoint only for a current Claim and existing sealed Intent.

    The same event/operation can have only one immutable Checkpoint across all
    generations. Local persistence succeeds only on a top-level Commit, not
    on a nested savepoint. No external effects or outcome probes are run.
    """
    _require_idle(connection)
    for name, value in (
        ("event_id", event_id),
        ("generation", generation),
        ("job_id", job_id),
        ("worker_id", worker_id),
        ("started_event_id", started_event_id),
    ):
        _positive(value, name)
    if type(attempts) is not int or attempts < 0:
        raise ValueError("attempts must be a nonnegative integer.")
    if type(expected_task_name) is not str or not expected_task_name.strip():
        raise ValueError("expected_task_name must be a nonempty Task name.")
    if type(expected_plan) is not PlanStructure or type(checkpoint) is not OperationCheckpoint:
        raise TypeError("expected_plan and checkpoint must be validated values.")
    expected_operation = next(
        (op for op in expected_plan.operations if op.operation_id == checkpoint.operation_id), None
    )
    if expected_operation is None:
        raise CheckpointHold("Checkpoint Operation is absent from expected Plan.")

    canonical = checkpoint.canonical_bytes()
    digest = checkpoint.checkpoint_digest
    operation_id = checkpoint.operation_id

    async with connection.transaction():
        try:
            await require_current_claim_and_sealed_plan(
                connection=connection,
                event_id=event_id,
                generation=generation,
                job_id=job_id,
                expected_task_name=expected_task_name,
                worker_id=worker_id,
                attempts=attempts,
                started_event_id=started_event_id,
                expected_plan=expected_plan,
            )
        except ClaimGuardHold as error:
            raise CheckpointHold(str(error)) from error

        cursor = await connection.execute(
            """
            SELECT created_generation, structure_digest, intent_version, intent_digest, intent_bytes
            FROM github_operation_intents
            WHERE event_id = %s AND operation_id = %s
            """,
            (event_id, operation_id),
        )
        raw_intent = await cursor.fetchone()
        if raw_intent is None:
            raise CheckpointHold("Operation Intent must be durably committed first.")
        try:
            intent = validate_intent_record(event_id, operation_id, raw_intent, "existing")
        except IntentIntegrityError as error:
            raise CheckpointHold("Operation Intent is unsupported or damaged.") from error
        try:
            stored_intent = OperationIntent.from_canonical_bytes(intent.intent_bytes)
        except (ValueError, TypeError) as error:
            raise CheckpointHold("Operation Intent contract is invalid.") from error
        if (
            intent.structure_digest != expected_plan.structure_digest
            or intent.intent_digest != checkpoint.intent_digest
            or intent.created_generation > generation
            or stored_intent.operation != expected_operation
        ):
            raise CheckpointHold("Checkpoint Intent/Plan evidence does not match.")

        # A dependent Intent is committed atomically with its field-level
        # lineage. Reject damaged/missing dependencies before appending its
        # Checkpoint; a stale Intent alone is not sufficient evidence.
        stores = await connection.execute("SELECT to_regclass('github_operation_lineages') IS NOT NULL")
        installed = await stores.fetchone() == (True,)
        if stored_intent.operation.depends_on and not installed:
            raise CheckpointHold("Dependent Intent lineage evidence store is unavailable.")
        if installed:
            linked = await connection.execute(
                """
                SELECT lineage_version, lineage_digest, lineage_bytes
                FROM github_operation_lineages
                WHERE event_id = %s AND operation_id = %s
                """,
                (event_id, operation_id),
            )
            raw_lineage = await linked.fetchone()
            if stored_intent.operation.depends_on:
                if raw_lineage is None:
                    raise CheckpointHold("Dependent Intent has no atomic lineage evidence.")
                # Import lazily to avoid the Checkpoint/Lineage store cycle.
                from .lineage_store import (
                    LineageHold,
                    LineageIntegrityError,
                    require_source_checkpoints,
                    validate_lineage_row,
                )

                try:
                    lineage_record = validate_lineage_row(event_id, operation_id, raw_lineage, "existing")
                    lineage = OperationLineage.from_canonical_bytes(lineage_record.lineage_bytes)
                    await require_source_checkpoints(
                        connection=connection,
                        event_id=event_id,
                        expected_plan=expected_plan,
                        intent=stored_intent,
                        lineage=lineage,
                    )
                except (LineageHold, LineageIntegrityError, ValueError, TypeError) as error:
                    raise CheckpointHold("Dependent Intent lineage is inconsistent or damaged.") from error
            elif raw_lineage is not None:
                raise CheckpointHold("Root Intent must not have lineage evidence.")

        cur = await connection.execute(
            """
            INSERT INTO github_operation_checkpoints
                (event_id, operation_id, recorded_generation, intent_digest,
                 checkpoint_version, checkpoint_digest, checkpoint_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id, operation_id) DO NOTHING
            RETURNING recorded_generation, intent_digest, checkpoint_version, checkpoint_digest, checkpoint_bytes
            """,
            (event_id, operation_id, generation, intent.intent_digest, CHECKPOINT_VERSION, digest, canonical),
        )
        row = await cur.fetchone()
        if row is not None:
            result = validate_checkpoint_row(event_id, operation_id, row, "recorded")
        else:
            existing = await connection.execute(
                """
                SELECT recorded_generation, intent_digest, checkpoint_version, checkpoint_digest, checkpoint_bytes
                FROM github_operation_checkpoints WHERE event_id = %s AND operation_id = %s
                """,
                (event_id, operation_id),
            )
            previous = await existing.fetchone()
            if previous is None:
                raise CheckpointIntegrityError("Existing Checkpoint could not be read back.")
            result = validate_checkpoint_row(event_id, operation_id, previous, "existing")
            if result.intent_digest != intent.intent_digest or result.checkpoint_bytes != canonical:
                raise CheckpointConflictError("Different Checkpoint already exists for the Event/Operation.")
        if result.recorded_generation < intent.created_generation:
            raise CheckpointIntegrityError("Stored Checkpoint predates its immutable Intent.")
    return result


async def load_operation_checkpoint(
    *, connection: AsyncConnection[tuple[Any, ...]], event_id: int, operation_id: str
) -> CheckpointRecord | None:
    """Read-only lookup; link integrity is checked, never execution safety."""
    _require_idle(connection)
    _positive(event_id, "event_id")
    if type(operation_id) is not str or not operation_id.strip():
        raise ValueError("operation_id must be nonempty.")
    cursor = await connection.execute(
        """
        SELECT c.recorded_generation, c.intent_digest, c.checkpoint_version,
               c.checkpoint_digest, c.checkpoint_bytes,
               i.created_generation, i.structure_digest, i.intent_version,
               i.intent_digest, i.intent_bytes,
               p.structure_version, p.structure_digest, p.structure_bytes
        FROM github_operation_checkpoints AS c
        LEFT JOIN github_operation_intents AS i
          ON (i.event_id, i.operation_id) = (c.event_id, c.operation_id)
        LEFT JOIN github_sealed_plans AS p ON p.event_id = c.event_id
        WHERE c.event_id = %s AND c.operation_id = %s
        """,
        (event_id, operation_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    record = validate_checkpoint_row(event_id, operation_id, row[:5], "existing")
    try:
        intent = validate_intent_record(event_id, operation_id, row[5:10], "existing")
        plan = validate_sealed_plan_row(event_id, row[10:], "existing")
        node = OperationIntent.from_canonical_bytes(intent.intent_bytes).operation
        structure = PlanStructure.from_canonical_bytes(plan.structure_bytes)
    except (IntentIntegrityError, ValueError, TypeError, RuntimeError) as error:
        raise CheckpointIntegrityError("Checkpoint references unsupported Intent/Plan.") from error
    if (
        intent.intent_digest != record.intent_digest
        or record.recorded_generation < intent.created_generation
        or intent.structure_digest != plan.structure_digest
        or not any(op == node for op in structure.operations)
    ):
        raise CheckpointIntegrityError("Checkpoint does not link to its sealed Intent/Plan.")

    # The Intent reader also validates the atomic dependent lineage and its
    # source Checkpoints (or rejects stray lineage on a Root Intent).
    # A valid local Checkpoint cannot outlive damaged Intent dependencies.
    try:
        linked = await load_operation_intent(connection=connection, event_id=event_id, operation_id=operation_id)
    except IntentIntegrityError as error:
        raise CheckpointIntegrityError("Checkpoint linked Intent lineage is invalid.") from error
    if linked is None or linked.intent_digest != record.intent_digest:
        raise CheckpointIntegrityError("Checkpoint linked Intent evidence is missing or inconsistent.")
    return record
