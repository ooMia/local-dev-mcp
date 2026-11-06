"""Attempt transaction races, disconnect and lost acknowledgement."""

import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    AttemptHold,
    AttemptRecord,
    IssueLabelHold,
    OperationAttempt,
    OperationIntent,
    bind_issue_add_label_contract,
    inspect_operation_recovery_evidence,
    load_operation_attempt,
    load_operation_checkpoint,
    record_operation_attempt,
)
from tests.support.gh_delivery import (
    DIGEST,
    connect,
)
from tests.support.gh_label_scope import assert_attempt_failure_is_atomic, prepared_label_scope

pytestmark = pytest.mark.integration


def test_label_scope_attempt_uncommitted() -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            options = case.options
            schema = case.schema
            receipt = case.receipt
            write_intent = case.write_intent
            writer = case.writer
            catalog = case.catalog
            api = case.api
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            # Missing Attempt DDL must fail closed, not infer "never POSTed".
            writer, catalog = await assert_attempt_failure_is_atomic(case)
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None

            async def insert_then_disconnect(
                *,
                connection: psycopg.AsyncConnection[tuple[Any, ...]],
                event_id: int,
                origin_generation: int,
                intent: OperationIntent,
            ) -> None:
                candidate = OperationAttempt(
                    event_id, intent.operation.operation_id, origin_generation, intent.intent_digest
                )
                async with connection.transaction():
                    cursor = await connection.execute(
                        """
                        INSERT INTO github_operation_attempts
                            (event_id, operation_id, origin_generation, intent_digest,
                             attempt_version, attempt_digest, attempt_bytes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING attempt_digest
                        """,
                        (
                            event_id,
                            candidate.operation_id,
                            origin_generation,
                            candidate.intent_digest,
                            candidate.version,
                            candidate.attempt_digest,
                            candidate.canonical_bytes(),
                        ),
                    )
                    assert await cursor.fetchone() == (candidate.attempt_digest,)
                    await connection.close()
                    raise psycopg.OperationalError("simulated disconnect before Attempt COMMIT")

            reads_before = api.get_calls
            with pytest.MonkeyPatch.context() as patcher:
                patcher.setattr(
                    "local_dev_mcp.tools.gh.delivery.issue_label_scope.record_operation_attempt",
                    insert_then_disconnect,
                )
                with pytest.raises(IssueLabelHold, match="write_scope_attempt_commit_failed"):
                    await writer.execute(write_intent)
            assert api.get_calls == reads_before + 1 and api.post_calls == 0
            with pytest.raises(IssueLabelHold, match="add_label_execution_already_attempted"):
                await writer.execute(write_intent)
            assert api.get_calls == reads_before + 1 and api.post_calls == 0
            async with await connect(options) as observer:
                await observer.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                assert (
                    await load_operation_attempt(connection=observer, event_id=receipt.event_id, operation_id="label")
                    is None
                )
                assert (
                    await load_operation_checkpoint(
                        connection=observer, event_id=receipt.event_id, operation_id="label"
                    )
                    is None
                )
                recovery = await inspect_operation_recovery_evidence(
                    connection=observer, catalog=catalog, event_id=receipt.event_id, operation_id="label"
                )
            assert recovery.status == "hold" and recovery.reason == "outcome_unknown"
            assert recovery.intent_digest == write_intent.intent_digest
            assert recovery.attempt_digest is None
            return

    asyncio.run(run())


def test_label_scope_attempt_race() -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            options = case.options
            schema = case.schema
            conn = case.conn
            receipt = case.receipt
            claim = case.claim
            write_intent = case.write_intent
            writer = case.writer
            catalog = case.catalog
            api = case.api
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            # Missing Attempt DDL must fail closed, not infer "never POSTed".
            writer, catalog = await assert_attempt_failure_is_atomic(case)
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            async with await connect(options) as first:
                async with await connect(options) as second:
                    for session in (first, second):
                        await session.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    start = asyncio.Event()
                    ready = 0

                    async def contender(session: psycopg.AsyncConnection[tuple[Any, ...]]) -> AttemptRecord:
                        nonlocal ready
                        ready += 1
                        if ready == 2:
                            start.set()
                        await asyncio.wait_for(start.wait(), timeout=5)
                        return await record_operation_attempt(
                            connection=session,
                            event_id=receipt.event_id,
                            origin_generation=claim.generation,
                            intent=write_intent,
                        )

                    outcomes = await asyncio.wait_for(
                        asyncio.gather(contender(first), contender(second), return_exceptions=True),
                        timeout=10,
                    )
            winners = [value for value in outcomes if isinstance(value, AttemptRecord)]
            rejected = [value for value in outcomes if isinstance(value, AttemptHold)]
            assert len(winners) == 1 and len(rejected) == 1
            assert "already exists" in str(rejected[0])
            durable = await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label")
            assert durable is not None and durable == winners[0]
            count = await conn.execute(
                "SELECT count(*) FROM github_operation_attempts WHERE event_id = %s AND operation_id = %s",
                (receipt.event_id, "label"),
            )
            assert await count.fetchone() == (1,)
            assert (
                await load_operation_checkpoint(connection=conn, event_id=receipt.event_id, operation_id="label")
                is None
            )
            recovery = await inspect_operation_recovery_evidence(
                connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
            )
            assert recovery.status == "hold" and recovery.reason == "outcome_unknown"
            assert recovery.attempt_digest == durable.attempt_digest
            reads_before = api.get_calls
            with pytest.raises(IssueLabelHold, match="attempt_already_recorded"):
                await writer.execute(write_intent)
            assert api.get_calls == reads_before and api.post_calls == 0
            return

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["attempt_ack_lost", "attempt_cancelled_after_commit"])
def test_label_scope_attempt_commit_acknowledgement(scenario: str) -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            options = case.options
            schema = case.schema
            conn = case.conn
            receipt = case.receipt
            claim = case.claim
            target = case.target
            intent = case.intent
            checkpoint = case.checkpoint
            write_intent = case.write_intent
            scope = case.scope
            writer = case.writer
            catalog = case.catalog
            fake_get = case.fake_get
            fake_post = case.fake_post
            api = case.api
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            # Missing Attempt DDL must fail closed, not infer "never POSTed".
            writer, catalog = await assert_attempt_failure_is_atomic(case)
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            committed = asyncio.Event()

            async def committed_without_ack(
                *,
                connection: psycopg.AsyncConnection[tuple[Any, ...]],
                event_id: int,
                origin_generation: int,
                intent: OperationIntent,
            ) -> None:
                await record_operation_attempt(
                    connection=connection,
                    event_id=event_id,
                    origin_generation=origin_generation,
                    intent=intent,
                )
                await connection.close()
                if scenario == "attempt_ack_lost":
                    raise psycopg.OperationalError("simulated lost Attempt commit acknowledgement")
                committed.set()
                await asyncio.Event().wait()  # Caller will cancel this pending Task.

            reads_before = api.get_calls
            with pytest.MonkeyPatch.context() as patcher:
                patcher.setattr(
                    "local_dev_mcp.tools.gh.delivery.issue_label_scope.record_operation_attempt",
                    committed_without_ack,
                )
                if scenario == "attempt_ack_lost":
                    with pytest.raises(IssueLabelHold, match="write_scope_attempt_commit_failed") as caught:
                        await writer.execute(write_intent)
                    assert isinstance(caught.value.__cause__, psycopg.OperationalError)
                else:

                    async def execute_in_task() -> object:
                        assert writer.execute is not None
                        return await writer.execute(write_intent)

                    pending = asyncio.create_task(execute_in_task())
                    try:
                        await asyncio.wait_for(committed.wait(), timeout=5)
                    finally:
                        pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
            assert api.get_calls == reads_before + 1  # Only the pre-POST GET ran.
            assert api.post_calls == 0 and api.observed_labels == {"bug"}
            with pytest.raises(IssueLabelHold, match="add_label_execution_already_attempted"):
                await writer.execute(write_intent)
            assert api.get_calls == reads_before + 1 and api.post_calls == 0
            durable = await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label")
            assert durable is not None and durable.attempt.origin_generation == claim.generation
            assert (
                await load_operation_checkpoint(connection=conn, event_id=receipt.event_id, operation_id="label")
                is None
            )
            recovery = await inspect_operation_recovery_evidence(
                connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
            )
            assert recovery.status == "hold" and recovery.reason == "outcome_unknown"
            assert recovery.attempt_digest == durable.attempt_digest
            async with await connect(options) as next_scope_conn:
                async with await connect(options) as next_attempt_conn:
                    for connection in (next_scope_conn, next_attempt_conn):
                        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    restarted_writer = bind_issue_add_label_contract(
                        intent=write_intent,
                        target=target,
                        label="triaged",
                        source_intent=intent,
                        source_checkpoint=checkpoint,
                        observe=fake_get,
                        add_label=fake_post,
                        write_scope=replace(scope, connection=next_scope_conn, attempt_connection=next_attempt_conn),
                        test_evidence_digest=DIGEST,
                    )
                    assert restarted_writer.execute is not None
                    reads_before_replay = api.get_calls
                    with pytest.raises(IssueLabelHold, match="attempt_already_recorded"):
                        await restarted_writer.execute(write_intent)
                    assert api.get_calls == reads_before_replay and api.post_calls == 0
            return

    asyncio.run(run())
