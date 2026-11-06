"""Explicit, append-only PostgreSQL sealing of immutable operation plan structure.

This is not a Worker, Claim implementation, operation Intent or authorization.
The owner must explicitly install PLANS_DDL in the designated PostgreSQL schema.
"""

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from .operation_plan import PLAN_STRUCTURE_VERSION, PlanStructure


class PlanConflictError(RuntimeError):
    """The event already has a different sealed structure."""


class PlanIntegrityError(RuntimeError):
    """Persisted structure evidence or receipt linkage is invalid."""


@dataclass(frozen=True, slots=True)
class SealedPlanRecord:
    event_id: int
    structure_version: str
    structure_digest: str
    structure_bytes: bytes
    disposition: Literal["sealed", "existing"]


def _require_idle(connection: AsyncConnection[tuple[Any, ...]], event_id: int) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Plan sealing requires an idle autocommit connection.")
    if type(event_id) is not int or event_id <= 0:
        raise ValueError("event_id must be a positive integer.")


def validate_sealed_plan_row(
    event_id: int, row: tuple[Any, ...], disposition: Literal["sealed", "existing"]
) -> SealedPlanRecord:
    version, digest, canonical = row
    if (
        version != PLAN_STRUCTURE_VERSION
        or type(canonical) is not bytes
        or not canonical
        or type(digest) is not str
        or digest != "sha256:" + hashlib.sha256(canonical).hexdigest()
    ):
        raise PlanIntegrityError("Unsupported or damaged sealed plan evidence.")
    try:
        PlanStructure.from_canonical_bytes(canonical)
    except (ValueError, TypeError) as error:
        raise PlanIntegrityError("Sealed plan bytes do not represent a supported canonical plan.") from error
    return SealedPlanRecord(
        event_id=event_id,
        structure_version=version,
        structure_digest=digest,
        structure_bytes=canonical,
        disposition=disposition,
    )


async def seal_plan_structure(
    *, connection: AsyncConnection[tuple[Any, ...]], event_id: int, plan: PlanStructure
) -> SealedPlanRecord:
    """Insert once; duplicate identical structure is safe, replacement is not.

    The receipt must exist and have an associated job. PostgreSQL's primary key
    serializes concurrent seals. This function commits its *own* transaction,
    not a caller-owned savepoint, and never executes the plan.
    """
    _require_idle(connection, event_id)
    if type(plan) is not PlanStructure:
        raise TypeError("plan must be a validated PlanStructure.")
    canonical = plan.canonical_bytes()
    digest = plan.structure_digest
    async with connection.transaction():
        receipt = await connection.execute("SELECT job_id FROM github_event_receipts WHERE event_id = %s", (event_id,))
        linked = await receipt.fetchone()
        if linked is None or type(linked[0]) is not int or linked[0] <= 0:
            raise PlanIntegrityError("A committed receipt with a linked job is required.")

        cursor = await connection.execute(
            """
            INSERT INTO github_sealed_plans (event_id, structure_version, structure_digest, structure_bytes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING structure_version, structure_digest, structure_bytes
            """,
            (event_id, PLAN_STRUCTURE_VERSION, digest, canonical),
        )
        inserted = await cursor.fetchone()
        if inserted is not None:
            record = validate_sealed_plan_row(event_id, inserted, "sealed")
        else:
            existing = await connection.execute(
                """
                SELECT structure_version, structure_digest, structure_bytes
                FROM github_sealed_plans WHERE event_id = %s
                """,
                (event_id,),
            )
            row = await existing.fetchone()
            if row is None:
                raise PlanIntegrityError("The sealed plan could not be read back.")
            record = validate_sealed_plan_row(event_id, row, "existing")
            if record.structure_bytes != canonical or record.structure_digest != digest:
                raise PlanConflictError("An event already has a different sealed plan structure.")
    return record


async def load_sealed_plan(*, connection: AsyncConnection[tuple[Any, ...]], event_id: int) -> SealedPlanRecord | None:
    """Read-only checksum and schema version validation; does not grant execution."""
    _require_idle(connection, event_id)
    cur = await connection.execute(
        "SELECT structure_version, structure_digest, structure_bytes FROM github_sealed_plans WHERE event_id = %s",
        (event_id,),
    )
    row = await cur.fetchone()
    return None if row is None else validate_sealed_plan_row(event_id, row, "existing")
