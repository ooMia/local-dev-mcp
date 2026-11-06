"""Q73 field-level Checkpoint lineage; called inside Intent's transaction.

A committed binding certifies an exact local evidence reference, not the
accuracy of any caller-provided GitHub verification claim or retry safety.
"""

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from .checkpoint_store import CheckpointIntegrityError, validate_checkpoint_row
from .immutable_intent import IntentIntegrityError, validate_intent_record
from .operation_checkpoint import OperationCheckpoint
from .operation_intent import OperationIntent
from .operation_lineage import LINEAGE_VERSION, OperationLineage
from .operation_plan import PlanStructure
from .sealed_plan import PlanIntegrityError, load_sealed_plan


class LineageHold(RuntimeError):
    """Required source Checkpoint evidence is missing or inconsistent."""


class LineageConflictError(RuntimeError):
    """A different immutable lineage already belongs to the Operation."""


class LineageIntegrityError(RuntimeError):
    """Persisted lineage is damaged, missing or unsupported."""


@dataclass(frozen=True, slots=True)
class LineageRecord:
    event_id: int
    operation_id: str
    lineage_digest: str
    lineage_bytes: bytes
    disposition: Literal["recorded", "existing"]


def validate_lineage_row(
    event_id: int,
    operation_id: str,
    row: tuple[Any, ...],
    disposition: Literal["recorded", "existing"],
) -> LineageRecord:
    version, digest, canonical = row
    if (
        version != LINEAGE_VERSION
        or type(digest) is not str
        or type(canonical) is not bytes
        or digest != "sha256:" + hashlib.sha256(canonical).hexdigest()
    ):
        raise LineageIntegrityError("Unsupported or corrupt Lineage evidence.")
    try:
        OperationLineage.from_canonical_bytes(canonical)
    except (ValueError, TypeError) as error:
        raise LineageIntegrityError("Lineage is not a supported canonical value.") from error
    return LineageRecord(event_id, operation_id, digest, canonical, disposition)


async def require_source_checkpoints(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    expected_plan: PlanStructure,
    intent: OperationIntent,
    lineage: OperationLineage,
) -> None:
    """Validate each immutable dependency and each field-level binding.

    Writers must hold Receipt/Job locks in their transaction. Read-only
    inspectors may instead use one consistent REPEATABLE READ snapshot.
    Source Checkpoints and source Intents are append-only; this function
    cannot establish the correctness of external GitHub effects.
    """
    stores = await connection.execute("SELECT to_regclass('github_operation_lineages') IS NOT NULL")
    if await stores.fetchone() != (True,):
        raise LineageHold("Required lineage evidence store is unavailable.")

    operations = {node.operation_id: node for node in expected_plan.operations}
    pending = [(intent, lineage)]
    inspected: set[str] = set()
    while pending:
        target, target_lineage = pending.pop()
        deps = {output.operation_id: output.checkpoint_digest for output in target.consumed_outputs}
        sources = {binding.source_operation_id for binding in target_lineage.bindings}
        if sources != set(target.operation.depends_on) or sources != set(deps):
            raise LineageHold("Lineage must bind every and only required dependency.")

        for source_id, expected_digest in deps.items():
            cursor = await connection.execute(
                """
                SELECT c.recorded_generation, c.intent_digest, c.checkpoint_version,
                       c.checkpoint_digest, c.checkpoint_bytes,
                       i.created_generation, i.structure_digest, i.intent_version,
                       i.intent_digest, i.intent_bytes
                FROM github_operation_checkpoints AS c
                JOIN github_operation_intents AS i
                  ON (i.event_id, i.operation_id) = (c.event_id, c.operation_id)
                WHERE c.event_id = %s AND c.operation_id = %s
                """,
                (event_id, source_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LineageHold("Dependency has no immutable Checkpoint.")
            try:
                checkpoint = validate_checkpoint_row(event_id, source_id, row[:5], "existing")
                source_intent = validate_intent_record(event_id, source_id, row[5:], "existing")
                source_model = OperationIntent.from_canonical_bytes(source_intent.intent_bytes)
                output_model = OperationCheckpoint.from_canonical_bytes(checkpoint.checkpoint_bytes)
            except (CheckpointIntegrityError, IntentIntegrityError, ValueError, TypeError) as error:
                raise LineageHold("Dependency Checkpoint/Intent evidence is damaged.") from error
            if (
                checkpoint.checkpoint_digest != expected_digest
                or checkpoint.recorded_generation < source_intent.created_generation
                or checkpoint.intent_digest != source_intent.intent_digest
                or source_intent.structure_digest != expected_plan.structure_digest
                or source_model.operation != operations.get(source_id)
            ):
                raise LineageHold("Dependency Checkpoint does not match the sealed source Intent/Plan.")
            fields = {field.field_id: field.value_digest for field in output_model.fields}
            for binding in target_lineage.bindings:
                if binding.source_operation_id != source_id:
                    continue
                if (
                    binding.source_checkpoint_digest != checkpoint.checkpoint_digest
                    or binding.source_schema_version != output_model.output_schema_version
                    or fields.get(binding.source_field_id) != binding.source_field_digest
                ):
                    raise LineageHold("Bound output field/schema/digest is not present in the source Checkpoint.")

            # A valid Checkpoint is insufficient if its own dependent Intent
            # lost its atomic lineage. Walk the sealed DAG to its roots.
            # Shared ancestors are queued once; each direct binding is checked.
            if source_id in inspected:
                continue
            inspected.add(source_id)
            upstream = await connection.execute(
                """
                SELECT lineage_version, lineage_digest, lineage_bytes
                FROM github_operation_lineages
                WHERE event_id = %s AND operation_id = %s
                """,
                (event_id, source_id),
            )
            upstream_row = await upstream.fetchone()
            if source_model.operation.depends_on:
                if upstream_row is None:
                    raise LineageHold("Dependent source Intent has no atomic lineage.")
                try:
                    record = validate_lineage_row(event_id, source_id, upstream_row, "existing")
                    upstream_lineage = OperationLineage.from_canonical_bytes(record.lineage_bytes)
                except (LineageIntegrityError, ValueError, TypeError) as error:
                    raise LineageHold("Dependent source lineage evidence is damaged.") from error
                pending.append((source_model, upstream_lineage))
            elif upstream_row is not None:
                raise LineageHold("Root source Intent must not have lineage evidence.")


async def commit_intent_lineage(
    *,
    connection: AsyncConnection[tuple[Any, ...]],
    event_id: int,
    operation_id: str,
    lineage: OperationLineage,
    intent_inserted: bool,
) -> LineageRecord:
    """Append lineage with the Intent, never backfill or replace on retry."""
    canonical = lineage.canonical_bytes()
    digest = lineage.lineage_digest
    if intent_inserted:
        cursor = await connection.execute(
            """
            INSERT INTO github_operation_lineages
                (event_id, operation_id, lineage_version, lineage_digest, lineage_bytes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (event_id, operation_id) DO NOTHING
            RETURNING lineage_version, lineage_digest, lineage_bytes
            """,
            (event_id, operation_id, LINEAGE_VERSION, digest, canonical),
        )
        row = await cursor.fetchone()
        if row is None:
            raise LineageIntegrityError("New Intent already had a lineage record.")
        return validate_lineage_row(event_id, operation_id, row, "recorded")
    cursor = await connection.execute(
        """
        SELECT lineage_version, lineage_digest, lineage_bytes FROM github_operation_lineages
        WHERE event_id = %s AND operation_id = %s
        """,
        (event_id, operation_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise LineageIntegrityError("Existing Intent has no atomic lineage evidence.")
    result = validate_lineage_row(event_id, operation_id, row, "existing")
    if result.lineage_bytes != canonical or result.lineage_digest != digest:
        raise LineageConflictError("Immutable source field bindings differ on retry.")
    return result


async def load_operation_lineage(
    *, connection: AsyncConnection[tuple[Any, ...]], event_id: int, operation_id: str
) -> LineageRecord | None:
    """Read-only evidence lookup. Does not prove current source state."""
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Lineage lookup requires an idle autocommit connection.")
    if type(event_id) is not int or event_id <= 0:
        raise ValueError("event_id must be positive.")
    if type(operation_id) is not str or not operation_id.strip():
        raise ValueError("operation_id must be nonempty.")
    cursor = await connection.execute(
        """
        SELECT l.lineage_version, l.lineage_digest, l.lineage_bytes,
               i.created_generation, i.structure_digest, i.intent_version,
               i.intent_digest, i.intent_bytes
        FROM github_operation_lineages AS l
        LEFT JOIN github_operation_intents AS i
          ON (i.event_id, i.operation_id) = (l.event_id, l.operation_id)
        WHERE l.event_id = %s AND l.operation_id = %s
        """,
        (event_id, operation_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    record = validate_lineage_row(event_id, operation_id, row[:3], "existing")
    try:
        intent_record = validate_intent_record(event_id, operation_id, row[3:8], "existing")
        intent = OperationIntent.from_canonical_bytes(intent_record.intent_bytes)
        lineage = OperationLineage.from_canonical_bytes(record.lineage_bytes)
    except (IntentIntegrityError, TypeError, ValueError) as error:
        raise LineageIntegrityError("Lineage has an invalid linked Intent.") from error
    if (
        not intent.operation.depends_on
        or {b.source_operation_id for b in lineage.bindings} != set(intent.operation.depends_on)
        or any(
            not any(
                dep.operation_id == b.source_operation_id and dep.checkpoint_digest == b.source_checkpoint_digest
                for dep in intent.consumed_outputs
            )
            for b in lineage.bindings
        )
    ):
        raise LineageIntegrityError("Lineage is inconsistent with its dependent Intent.")
    try:
        sealed = await load_sealed_plan(connection=connection, event_id=event_id)
        if sealed is None:
            raise LineageIntegrityError("Source Plan is missing.")
        structure = PlanStructure.from_canonical_bytes(sealed.structure_bytes)
        if intent_record.structure_digest != sealed.structure_digest or not any(
            operation == intent.operation for operation in structure.operations
        ):
            raise LineageIntegrityError("Dependent Intent differs from its sealed Plan.")
        await require_source_checkpoints(
            connection=connection,
            event_id=event_id,
            expected_plan=structure,
            intent=intent,
            lineage=lineage,
        )
    except (PlanIntegrityError, LineageHold, ValueError, TypeError) as error:
        raise LineageIntegrityError("Lineage source evidence is missing or damaged.") from error
    return record
