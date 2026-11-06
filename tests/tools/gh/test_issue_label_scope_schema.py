"""Missing or incompatible Attempt schema prevents external calls."""

import asyncio
from uuid import uuid4

import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    IssueLabelHold,
    inspect_operation_recovery_evidence,
    load_operation_attempt,
)
from tests.support.gh_label_scope import prepared_label_scope

pytestmark = pytest.mark.integration


def test_label_scope_attempt_wrong_schema() -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            conn = case.conn
            attempt_conn = case.attempt_conn
            receipt = case.receipt
            write_intent = case.write_intent
            writer = case.writer
            api = case.api
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            wrong_schema = "b3s_wrong_attempt_" + uuid4().hex
            await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(wrong_schema)))
            try:
                assert attempt_conn is not None
                await attempt_conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(wrong_schema)))
                reads_before = api.get_calls
                with pytest.raises(IssueLabelHold, match="attempt_connection_mismatch"):
                    await writer.concurrency.guard(write_intent)
                with pytest.raises(IssueLabelHold, match="attempt_connection_mismatch"):
                    await writer.execute(write_intent)
                assert api.get_calls == reads_before and api.post_calls == 0
                assert (
                    await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label")
                    is None
                )
            finally:
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(wrong_schema)))
            return

    asyncio.run(run())


@pytest.mark.parametrize(
    "scenario",
    [
        "attempt_generation_fk_missing",
        "attempt_intent_fk_missing",
        "attempt_pk_missing",
        "attempt_schema_incomplete",
        "attempt_trigger_disabled",
    ],
)
def test_label_scope_attempt_store_constraints(scenario: str) -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            conn = case.conn
            receipt = case.receipt
            write_intent = case.write_intent
            writer = case.writer
            catalog = case.catalog
            api = case.api
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            if scenario == "attempt_schema_incomplete":
                await conn.execute("ALTER TABLE github_operation_attempts DROP COLUMN attempt_version")
            elif scenario == "attempt_pk_missing":
                await conn.execute(
                    "ALTER TABLE github_operation_attempts DROP CONSTRAINT github_operation_attempts_pkey"
                )
            elif scenario == "attempt_intent_fk_missing":
                await conn.execute("ALTER TABLE github_operation_attempts DROP CONSTRAINT github_attempt_intent_fk")
            elif scenario == "attempt_generation_fk_missing":
                await conn.execute("ALTER TABLE github_operation_attempts DROP CONSTRAINT github_attempt_generation_fk")
            else:
                await conn.execute(
                    "ALTER TABLE github_operation_attempts DISABLE TRIGGER github_operation_attempt_immutable"
                )
            reads_before = api.get_calls
            with pytest.raises(IssueLabelHold, match="attempt_store_unavailable"):
                await writer.concurrency.guard(write_intent)
            with pytest.raises(IssueLabelHold, match="attempt_store_unavailable"):
                assert writer.execute is not None
                await writer.execute(write_intent)
            assert api.get_calls == reads_before and api.post_calls == 0
            recovery = await inspect_operation_recovery_evidence(
                connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
            )
            assert recovery.status == "hold" and recovery.reason == "evidence_store_unavailable"
            assert recovery.attempt_digest is None
            count = await conn.execute(
                "SELECT count(*) FROM github_operation_attempts WHERE event_id = %s",
                (receipt.event_id,),
            )
            assert await count.fetchone() == (0,)
            return

    asyncio.run(run())
