"""Transactional, append-only Operation Intent evidence after a current claim.

An Intent is identified by (event_id, operation_id) across generations.
Neither this record nor a successful transaction grants remote execution rights.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from .claim_guard import ClaimGuardHold, require_current_claim_and_sealed_plan
from .operation_intent import INTENT_VERSION, OperationIntent
from .operation_lineage import OperationLineage
from .operation_plan import PlanStructure
from .sealed_plan import PlanIntegrityError, validate_sealed_plan_row


class IntentHold(RuntimeError):
    """Current Job/Claim/Plan or required dependency evidence cannot be trusted."""


class IntentConflictError(RuntimeError):
    """The event + Operation already has different immutable Intent evidence."""


class IntentIntegrityError(RuntimeError):
    """Persisted Intent evidence is corrupt or uses an unsupported contract."""


@dataclass(frozen=True, slots=True)
class IntentRecord:
    event_id: int
    operation_id: str
    created_generation: int
    structure_digest: str
    intent_digest: str
    intent_bytes: bytes
    disposition: Literal["recorded", "existing"]


def _require_idle(connection: AsyncConnection[tuple[Any, ...]]) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Intent persistence requires an idle autocommit connection.")


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def validate_intent_record(
    event_id: int, operation_id: str, row: tuple[Any, ...], disposition: Literal["recorded", "existing"]
) -> IntentRecord:
    generation, structure_digest, version, digest, raw = row
    if (
        type(generation) is not int
        or generation <= 0
        or type(structure_digest) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", structure_digest) is None
        or version != INTENT_VERSION
        or type(digest) is not str
        or type(raw) is not bytes
        or digest != "sha256:" + hashlib.sha256(raw).hexdigest()
    ):
        raise IntentIntegrityError("Unsupported or corrupted persisted Intent evidence.")
    try:
        parsed = OperationIntent.from_canonical_bytes(raw)
    except (ValueError, TypeError) as error:
        raise IntentIntegrityError("Stored Intent is not a supported canonical value.") from error
    if parsed.operation.operation_id != operation_id:
        raise IntentIntegrityError("Stored Intent identity conflicts with its primary key.")
    return IntentRecord(event_id, operation_id, generation, structure_digest, digest, raw, disposition)


async def record_operation_intent(
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
    intent: OperationIntent,
    lineage: OperationLineage | None = None,
) -> IntentRecord:
    """Commit immutable evidence iff current worker/generation and sealed plan match.

    Lock ordering matches Claim Generation: Receipt first, then Procrastinate Job.
    The same transaction validates current ownership, newest generation and
    the sealed plan before inserting. Dependent Operations are held until
    immutable Checkpoint Output lineage can be checked against durable rows.
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
        raise ValueError("expected_task_name must be a nonempty registered Task.")
    if type(expected_plan) is not PlanStructure or type(intent) is not OperationIntent:
        raise TypeError("expected_plan and intent must be validated immutable values.")

    operation = next((op for op in expected_plan.operations if op.operation_id == intent.operation.operation_id), None)
    if operation is None or operation != intent.operation:
        raise IntentHold("Intent Operation is absent or differs from the expected Plan.")
    if lineage is not None and type(lineage) is not OperationLineage:
        raise TypeError("lineage must be a supported immutable OperationLineage.")
    if operation.depends_on and lineage is None:
        raise IntentHold("Dependent Operation requires explicit Checkpoint field lineage.")
    if not operation.depends_on and lineage is not None:
        raise IntentHold("Root Operation must not persist a lineage record.")
    if lineage is not None and (
        {binding.source_operation_id for binding in lineage.bindings} != set(operation.depends_on)
    ):
        raise IntentHold("Lineage sources must match exactly the sealed Operation dependencies.")

    canonical = intent.canonical_bytes()
    digest = intent.intent_digest
    structure_digest = expected_plan.structure_digest

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
            raise IntentHold(str(error)) from error

        if lineage is not None:
            # Imported lazily to avoid circular imports between the append-only
            # Intent and Checkpoint stores.
            from .lineage_store import LineageHold, require_source_checkpoints

            try:
                await require_source_checkpoints(
                    connection=connection,
                    event_id=event_id,
                    expected_plan=expected_plan,
                    intent=intent,
                    lineage=lineage,
                )
            except LineageHold as error:
                raise IntentHold(str(error)) from error

        inserted = await connection.execute(
            """
            INSERT INTO github_operation_intents
                (event_id, operation_id, created_generation, structure_digest,
                 intent_version, intent_digest, intent_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id, operation_id) DO NOTHING
            RETURNING created_generation, structure_digest, intent_version, intent_digest, intent_bytes
            """,
            (event_id, operation.operation_id, generation, structure_digest, INTENT_VERSION, digest, canonical),
        )
        new_row = await inserted.fetchone()
        if new_row is not None:
            result = validate_intent_record(event_id, operation.operation_id, new_row, "recorded")
        else:
            existing = await connection.execute(
                """
                SELECT created_generation, structure_digest, intent_version, intent_digest, intent_bytes
                FROM github_operation_intents WHERE event_id = %s AND operation_id = %s
                """,
                (event_id, operation.operation_id),
            )
            existing_row = await existing.fetchone()
            if existing_row is None:
                raise IntentIntegrityError("Existing Intent could not be read back.")
            result = validate_intent_record(event_id, operation.operation_id, existing_row, "existing")
            if (
                result.structure_digest != structure_digest
                or result.intent_bytes != canonical
                or result.intent_digest != digest
            ):
                raise IntentConflictError("Immutable Intent is different for the same Event and Operation.")
        if lineage is not None:
            from .lineage_store import commit_intent_lineage

            await commit_intent_lineage(
                connection=connection,
                event_id=event_id,
                operation_id=operation.operation_id,
                lineage=lineage,
                intent_inserted=result.disposition == "recorded",
            )
    return result


async def load_operation_intent(
    *, connection: AsyncConnection[tuple[Any, ...]], event_id: int, operation_id: str
) -> IntentRecord | None:
    """Read-only Intent/Sealed Plan integrity check; never an execution permit."""
    _require_idle(connection)
    _positive(event_id, "event_id")
    if type(operation_id) is not str or not operation_id.strip():
        raise ValueError("operation_id must be nonempty.")
    cursor = await connection.execute(
        """
        SELECT i.created_generation, i.structure_digest, i.intent_version,
               i.intent_digest, i.intent_bytes,
               p.structure_version, p.structure_digest, p.structure_bytes
        FROM github_operation_intents AS i
        LEFT JOIN github_sealed_plans AS p ON p.event_id = i.event_id
        WHERE i.event_id = %s AND i.operation_id = %s
        """,
        (event_id, operation_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    record = validate_intent_record(event_id, operation_id, row[:5], "existing")
    try:
        sealed = validate_sealed_plan_row(event_id, row[5:], "existing")
    except (PlanIntegrityError, ValueError, TypeError) as error:
        raise IntentIntegrityError("Stored Intent has no valid sealed Plan.") from error
    if sealed.structure_digest != record.structure_digest:
        raise IntentIntegrityError("Stored Intent does not reference the sealed Plan.")
    sealed_structure = PlanStructure.from_canonical_bytes(sealed.structure_bytes)
    stored_operation = OperationIntent.from_canonical_bytes(record.intent_bytes).operation
    if not any(operation == stored_operation for operation in sealed_structure.operations):
        raise IntentIntegrityError("Stored Intent Operation is absent from its sealed Plan.")

    # Dependent Intent and field lineage are written atomically. A damaged
    # or missing lineage must not be interpreted as a valid persisted Intent.
    # Root-only storage does not require installing the optional evidence DDL.
    stores = await connection.execute(
        "SELECT to_regclass('github_operation_lineages') IS NOT NULL, "
        "to_regclass('github_operation_checkpoints') IS NOT NULL"
    )
    present = await stores.fetchone()
    lineage_installed = present is not None and present[0] is True
    checkpoint_installed = present is not None and present[1] is True
    if stored_operation.depends_on:
        if not (lineage_installed and checkpoint_installed):
            raise IntentIntegrityError("Dependent Intent evidence stores are not installed.")
        from .lineage_store import LineageIntegrityError, load_operation_lineage

        try:
            linked = await load_operation_lineage(connection=connection, event_id=event_id, operation_id=operation_id)
        except LineageIntegrityError as error:
            raise IntentIntegrityError("Dependent Intent lineage is damaged.") from error
        if linked is None:
            raise IntentIntegrityError("Dependent Intent has no atomic lineage record.")
    elif lineage_installed:
        unexpected = await connection.execute(
            "SELECT 1 FROM github_operation_lineages WHERE event_id = %s AND operation_id = %s",
            (event_id, operation_id),
        )
        if await unexpected.fetchone() is not None:
            raise IntentIntegrityError("Root Intent has unexpected lineage evidence.")
    return record
