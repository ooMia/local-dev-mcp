"""Read-only PostgreSQL schema preflight for GitHub durable admission.

This guard does not install, migrate, repair or touch stored event data.
A mismatched schema must be held before receipt/job insertion.
"""

from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

_RECEIPT_COLUMNS: dict[str, tuple[str, str]] = {
    "event_id": ("int8", "NO"),
    "source_instance_id": ("text", "NO"),
    "event_key": ("text", "NO"),
    "event_type": ("text", "NO"),
    "event_schema_version": ("text", "NO"),
    "payload_digest": ("text", "NO"),
    "observed_at": ("timestamptz", "NO"),
    "envelope_schema_version": ("text", "NO"),
    "repository": ("text", "NO"),
    "repository_id": ("int8", "NO"),
    "target_kind": ("text", "NO"),
    "target_number": ("int4", "NO"),
    "action": ("text", "NO"),
    "target_updated_at": ("text", "NO"),
    "job_id": ("int8", "YES"),
}
_QUEUE_COLUMNS: dict[str, tuple[str, str]] = {"id": ("int8", "NO"), "args": ("jsonb", "NO")}


@dataclass(frozen=True, slots=True)
class SchemaReadiness:
    """Non-sensitive read-only result. Missing or incompatible objects fail closed."""

    ready: bool
    issues: tuple[str, ...]


async def inspect_delivery_schema(*, connection: AsyncConnection[tuple[Any, ...]]) -> SchemaReadiness:
    """Check table/column contracts and the source-qualified unique constraint.

    Uses only pg_catalog and information_schema. It never modifies DB state.
    The caller must select the intended schema on an idle autocommit connection.
    """
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Schema inspection requires an idle autocommit connection.")
    schema_query = await connection.execute("SELECT current_schema()")
    schema_row = await schema_query.fetchone()
    if schema_row is None or not isinstance(schema_row[0], str):
        return SchemaReadiness(ready=False, issues=("schema_missing",))
    schema = schema_row[0]

    objects = await connection.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s
          AND c.relname IN ('github_event_receipts', 'procrastinate_jobs')
        """,
        (schema,),
    )
    found = dict(await objects.fetchall())
    issues: list[str] = []
    for name in ("github_event_receipts", "procrastinate_jobs"):
        if found.get(name) not in ("r", "p"):
            issues.append(f"{name}_missing_or_invalid")

    columns = await connection.execute(
        """
        SELECT table_name, column_name, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name IN ('github_event_receipts', 'procrastinate_jobs')
        """,
        (schema,),
    )
    actual = {(table, col): (type_name, nullable) for table, col, type_name, nullable in await columns.fetchall()}
    for table, expected in (("github_event_receipts", _RECEIPT_COLUMNS), ("procrastinate_jobs", _QUEUE_COLUMNS)):
        if table not in found:
            continue
        if any(actual.get((table, column)) != spec for column, spec in expected.items()):
            issues.append(f"{table}_columns_incompatible")

    constraints = await connection.execute(
        """
        SELECT con.contype, pg_catalog.pg_get_constraintdef(con.oid)
        FROM pg_catalog.pg_constraint con
        JOIN pg_catalog.pg_class rel ON con.conrelid = rel.oid
        JOIN pg_catalog.pg_namespace ns ON rel.relnamespace = ns.oid
        WHERE ns.nspname = %s AND rel.relname = 'github_event_receipts'
          AND con.conname = 'github_event_identity'
        """,
        (schema,),
    )
    constraint = await constraints.fetchone()
    if constraint != ("u", "UNIQUE (source_instance_id, event_key)"):
        issues.append("receipt_unique_constraint_missing")
    return SchemaReadiness(ready=not issues, issues=tuple(issues))


async def require_delivery_schema(*, connection: AsyncConnection[tuple[Any, ...]]) -> None:
    """Fail closed with a non-sensitive exception if the DB cannot admit jobs."""
    status = await inspect_delivery_schema(connection=connection)
    if not status.ready:
        raise SchemaNotReady("GitHub delivery database schema is not ready.")


class SchemaNotReady(RuntimeError):
    """Read-only schema preflight found an unsupported admission contract."""
