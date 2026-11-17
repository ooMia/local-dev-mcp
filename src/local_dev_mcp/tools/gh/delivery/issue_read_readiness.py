"""Read-only schema preflight for the opt-in GitHub Issue read Worker.

This is operator diagnostics, not permission to run Jobs or proof of
concurrent DDL safety. No Worker is registered, claimed, or started.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from .readiness import SchemaReadiness, inspect_delivery_schema

ConnectionFactory = Callable[[], Awaitable[AsyncConnection[tuple[Any, ...]]]]

# Type and nullability requirements for the four read-Worker evidence tables.
# The receiver already checks the Receipt and Job table separately.
_EVIDENCE_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "github_sealed_plans": {
        "event_id": ("int8", "NO"),
        "structure_version": ("text", "NO"),
        "structure_digest": ("text", "NO"),
        "structure_bytes": ("bytea", "NO"),
    },
    "github_delivery_claim_generations": {
        "event_id": ("int8", "NO"),
        "generation": ("int8", "NO"),
        "job_id": ("int8", "NO"),
        "started_event_id": ("int8", "NO"),
        "worker_id": ("int8", "NO"),
        "attempts": ("int4", "NO"),
    },
    "github_operation_intents": {
        "event_id": ("int8", "NO"),
        "operation_id": ("text", "NO"),
        "created_generation": ("int8", "NO"),
        "structure_digest": ("text", "NO"),
        "intent_version": ("text", "NO"),
        "intent_digest": ("text", "NO"),
        "intent_bytes": ("bytea", "NO"),
    },
    "github_operation_checkpoints": {
        "event_id": ("int8", "NO"),
        "operation_id": ("text", "NO"),
        "recorded_generation": ("int8", "NO"),
        "intent_digest": ("text", "NO"),
        "checkpoint_version": ("text", "NO"),
        "checkpoint_digest": ("text", "NO"),
        "checkpoint_bytes": ("bytea", "NO"),
    },
}
_PROCRASTINATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "procrastinate_workers": ("id",),
    "procrastinate_events": ("id", "job_id", "type"),
    "procrastinate_jobs": (
        "id",
        "args",
        "task_name",
        "status",
        "worker_id",
        "attempts",
        "abort_requested",
    ),
}
# The Worker uses these fields with fixed Python/SQL types. Procrastinate's
# enum columns are checked for presence, not a hard-coded enum type name.
_PROCRASTINATE_TYPED_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "procrastinate_workers": {"id": ("int8", "NO")},
    "procrastinate_events": {"id": ("int8", "NO"), "job_id": ("int8", "NO")},
    "procrastinate_jobs": {
        "id": ("int8", "NO"),
        "args": ("jsonb", "NO"),
        "task_name": ("varchar", "NO"),
        "worker_id": ("int8", "YES"),
        "attempts": ("int4", "NO"),
        "abort_requested": ("bool", "NO"),
    },
}
_PROCRASTINATE_PRIMARY_KEYS: dict[str, str] = dict.fromkeys(_PROCRASTINATE_COLUMNS, "PRIMARY KEY (id)")
_PRIMARY_KEYS: dict[str, str] = {
    "github_sealed_plans": "PRIMARY KEY (event_id)",
    "github_delivery_claim_generations": "PRIMARY KEY (event_id, generation)",
    "github_operation_intents": "PRIMARY KEY (event_id, operation_id)",
    "github_operation_checkpoints": "PRIMARY KEY (event_id, operation_id)",
}
# Exact FK definitions restrict checks to the existing v1 Worker DDL, not a
# generic "has any foreign key" approximation.
_FK_RECEIPT = "FOREIGN KEY (event_id) REFERENCES github_event_receipts(event_id)"
_FOREIGN_KEYS: dict[str, dict[str, str]] = {
    "github_sealed_plans": {
        "github_sealed_plans_event_id_fkey": _FK_RECEIPT,
    },
    "github_delivery_claim_generations": {
        "github_delivery_claim_generations_event_id_fkey": _FK_RECEIPT,
    },
    "github_operation_intents": {
        "github_operation_intents_event_id_fkey": _FK_RECEIPT,
        "github_intent_generation_fk": "FOREIGN KEY (event_id, created_generation) REFERENCES "
        "github_delivery_claim_generations(event_id, generation)",
    },
    "github_operation_checkpoints": {
        "github_checkpoint_intent_fk": "FOREIGN KEY (event_id, operation_id) REFERENCES "
        "github_operation_intents(event_id, operation_id)",
        "github_checkpoint_generation_fk": "FOREIGN KEY (event_id, recorded_generation) REFERENCES "
        "github_delivery_claim_generations(event_id, generation)",
    },
}
_IMMUTABLE_TRIGGERS: dict[str, tuple[str, str]] = {
    "github_sealed_plans": ("github_sealed_plan_immutable", "github_reject_sealed_plan_change"),
    "github_delivery_claim_generations": ("github_claim_generation_immutable", "github_reject_claim_generation_change"),
    "github_operation_intents": ("github_operation_intent_immutable", "github_reject_operation_intent_change"),
    "github_operation_checkpoints": (
        "github_operation_checkpoint_immutable",
        "github_reject_operation_checkpoint_change",
    ),
}


async def inspect_issue_read_worker_schema(*, connection: AsyncConnection[tuple[Any, ...]]) -> SchemaReadiness:
    """Inspect the current schema without claiming, executing, or mutating it.

    Verifies minimum read/write column shape, uniqueness, critical FKs and
    append-only trigger bindings. Does not certify function bodies, privileges,
    PostgreSQL session replica role, or concurrent DDL changes.
    """
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Worker schema preflight requires an idle autocommit connection.")
    delivery = await inspect_delivery_schema(connection=connection)
    issues = list(delivery.issues)

    schema_cursor = await connection.execute("SELECT current_schema()")
    row = await schema_cursor.fetchone()
    if row is None or type(row[0]) is not str:
        if "schema_missing" not in issues:
            issues.append("schema_missing")
        return SchemaReadiness(False, tuple(issues))
    schema: str = row[0]
    names = tuple(_EVIDENCE_COLUMNS) + tuple(_PROCRASTINATE_COLUMNS)

    objects = await connection.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = ANY(%s)
        """,
        (schema, list(names)),
    )
    kinds = dict(await objects.fetchall())
    columns = await connection.execute(
        """
        SELECT table_name, column_name, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = ANY(%s)
        """,
        (schema, list(names)),
    )
    found = {(t, c): (kind, nullable) for t, c, kind, nullable in await columns.fetchall()}

    constraints = await connection.execute(
        """
        SELECT rel.relname, k.contype, k.conname, pg_catalog.pg_get_constraintdef(k.oid)
        FROM pg_catalog.pg_constraint AS k
        JOIN pg_catalog.pg_class AS rel ON rel.oid = k.conrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = rel.relnamespace
        WHERE n.nspname = %s AND rel.relname = ANY(%s)
        """,
        (schema, list(names)),
    )
    saved = {(t, kind, name): definition for t, kind, name, definition in await constraints.fetchall()}
    triggers = await connection.execute(
        """
        SELECT rel.relname, tg.tgname, tg.tgtype, tg.tgenabled,
               func.proname, funcs.nspname
        FROM pg_catalog.pg_trigger AS tg
        JOIN pg_catalog.pg_class AS rel ON rel.oid = tg.tgrelid
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = rel.relnamespace
        JOIN pg_catalog.pg_proc AS func ON func.oid = tg.tgfoid
        JOIN pg_catalog.pg_namespace AS funcs ON funcs.oid = func.pronamespace
        WHERE ns.nspname = %s AND rel.relname = ANY(%s)
          AND NOT tg.tgisinternal
        """,
        (schema, list(_EVIDENCE_COLUMNS)),
    )
    active = {
        (table, trigger): (kind, enabled, func, proc_schema)
        for table, trigger, kind, enabled, func, proc_schema in await triggers.fetchall()
    }

    for table, expected in _EVIDENCE_COLUMNS.items():
        if kinds.get(table) not in ("r", "p"):
            issues.append(f"{table}_missing_or_invalid")
            continue
        if any(found.get((table, col)) != spec for col, spec in expected.items()):
            issues.append(f"{table}_columns_incompatible")
        if not any(
            definition == _PRIMARY_KEYS[table]
            for (rel, kind, _), definition in saved.items()
            if rel == table and kind == "p"
        ):
            issues.append(f"{table}_primary_key_missing")
        if any(saved.get((table, "f", name)) != definition for name, definition in _FOREIGN_KEYS[table].items()):
            issues.append(f"{table}_foreign_key_missing")
        trigger, proc = _IMMUTABLE_TRIGGERS[table]
        if active.get((table, trigger)) not in ((27, "O", proc, schema), (27, "A", proc, schema)):
            issues.append(f"{table}_immutable_trigger_missing")

    for table, required in _PROCRASTINATE_COLUMNS.items():
        if kinds.get(table) not in ("r", "p"):
            issues.append(f"{table}_missing_or_invalid")
            continue
        if any((table, col) not in found for col in required) or any(
            found.get((table, col)) != spec for col, spec in _PROCRASTINATE_TYPED_COLUMNS[table].items()
        ):
            issues.append(f"{table}_columns_incompatible")
        if not any(
            definition == _PROCRASTINATE_PRIMARY_KEYS[table]
            for (rel, kind, _), definition in saved.items()
            if rel == table and kind == "p"
        ):
            issues.append(f"{table}_primary_key_missing")

    # A Claim's started event must be unique across generations, even when
    # the same Job is reused. The second UNIQUE key is part of its DDL.
    if kinds.get("github_delivery_claim_generations") in ("r", "p") and (
        saved.get(
            (
                "github_delivery_claim_generations",
                "u",
                "github_claim_started_unique",
            )
        )
        != "UNIQUE (event_id, started_event_id)"
    ):
        issues.append("github_delivery_claim_generations_started_unique_missing")

    return SchemaReadiness(ready=not issues, issues=tuple(issues))


async def inspect_issue_read_worker_database(*, connection_factory: ConnectionFactory) -> SchemaReadiness:
    """Explicit operator preflight; no Worker registration or schema install."""
    async with await connection_factory() as connection:
        return await inspect_issue_read_worker_schema(connection=connection)
