"""Read-only operator preflight for the opt-in Issue read Worker."""

import asyncio
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql

from local_dev_mcp.tools.gh.delivery.issue_read_readiness import (
    inspect_issue_read_worker_database,
    inspect_issue_read_worker_schema,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    CHECKPOINTS_DDL,
    CLAIM_GENERATIONS_DDL,
    INTENTS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from tests.support.gh_delivery import connect, connection_options, install_procrastinate_schema

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("healthy", None),
        ("missing_checkpoint_table", "github_operation_checkpoints_missing_or_invalid"),
        ("missing_plan_column", "github_sealed_plans_columns_incompatible"),
        ("missing_intent_fk", "github_operation_intents_foreign_key_missing"),
        ("disabled_checkpoint_trigger", "github_operation_checkpoints_immutable_trigger_missing"),
        ("missing_claim_unique", "github_delivery_claim_generations_started_unique_missing"),
        ("missing_procrastinate_events", "procrastinate_events_missing_or_invalid"),
        ("nullable_procrastinate_job_abort", "procrastinate_jobs_columns_incompatible"),
        ("missing_procrastinate_event_pk", "procrastinate_events_primary_key_missing"),
    ],
)
def test_issue_read_worker_preflight_rejects_incomplete_schema(fault: str, expected: str | None) -> None:
    options = connection_options()
    schema = "b3s_worker_ready_" + uuid4().hex

    async def factory() -> AsyncConnection[tuple[object, ...]]:
        connection = await connect(options)
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    async def run() -> None:
        async with await connect(options) as connection:
            try:
                await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(connection)
                for ddl in (RECEIPTS_DDL, PLANS_DDL, CLAIM_GENERATIONS_DDL, INTENTS_DDL, CHECKPOINTS_DDL):
                    await connection.execute(ddl)

                # Read-only inspection must be usable without Worker
                # registration, Job scheduling, credentials or GitHub calls.
                await connection.execute("SET default_transaction_read_only = on")
                ready = await inspect_issue_read_worker_schema(connection=connection)
                via_factory = await inspect_issue_read_worker_database(connection_factory=factory)
                assert ready == via_factory
                assert ready.ready and ready.issues == ()
                await connection.execute("SET default_transaction_read_only = off")

                if fault == "missing_checkpoint_table":
                    await connection.execute("DROP TABLE github_operation_checkpoints")
                elif fault == "missing_plan_column":
                    await connection.execute("ALTER TABLE github_sealed_plans DROP COLUMN structure_bytes")
                elif fault == "missing_intent_fk":
                    await connection.execute(
                        "ALTER TABLE github_operation_intents DROP CONSTRAINT github_intent_generation_fk"
                    )
                elif fault == "disabled_checkpoint_trigger":
                    await connection.execute(
                        "ALTER TABLE github_operation_checkpoints DISABLE TRIGGER github_operation_checkpoint_immutable"
                    )
                elif fault == "missing_claim_unique":
                    await connection.execute(
                        "ALTER TABLE github_delivery_claim_generations DROP CONSTRAINT github_claim_started_unique"
                    )
                elif fault == "missing_procrastinate_events":
                    await connection.execute("DROP TABLE procrastinate_events CASCADE")
                elif fault == "nullable_procrastinate_job_abort":
                    await connection.execute(
                        "ALTER TABLE procrastinate_jobs ALTER COLUMN abort_requested DROP NOT NULL"
                    )
                elif fault == "missing_procrastinate_event_pk":
                    await connection.execute(
                        "ALTER TABLE procrastinate_events DROP CONSTRAINT procrastinate_events_pkey"
                    )

                after = await inspect_issue_read_worker_database(connection_factory=factory)
                if expected is None:
                    assert after.ready and after.issues == ()
                else:
                    assert not after.ready
                    assert expected in after.issues
                job_count = await connection.execute("SELECT count(*) FROM procrastinate_jobs")
                assert await job_count.fetchone() == (0,)
            finally:
                await connection.execute("SET default_transaction_read_only = off")
                await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
