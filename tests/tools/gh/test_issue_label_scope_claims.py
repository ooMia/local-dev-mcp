"""Label scope ownership, process death, lock loss and normal completion."""

import asyncio
import json
import sys
from dataclasses import replace

import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    AttemptHold,
    IssueLabelHold,
    bind_issue_add_label_contract,
    inspect_operation_recovery_evidence,
    inspect_running_claim,
    issue_label_lock_key,
    load_latest_claim_generation,
    load_operation_attempt,
    load_operation_checkpoint,
    record_claim_generation,
    record_operation_attempt,
    record_operation_checkpoint,
)
from local_dev_mcp.tools.gh.webhook import VerifiedTarget
from tests.support.gh_delivery import (
    DIGEST,
    connect,
    requeue_job,
)
from tests.support.gh_label_scope import assert_attempt_failure_is_atomic, prepared_label_scope

pytestmark = pytest.mark.integration


def test_label_scope_regenerated() -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            conn = case.conn
            receipt = case.receipt
            worker_id = case.worker_id
            started = case.started
            claim = case.claim
            target = case.target
            intent = case.intent
            checkpoint = case.checkpoint
            write_intent = case.write_intent
            scope = case.scope
            writer = case.writer
            fake_get = case.fake_get
            fake_post = case.fake_post
            api = case.api
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            await requeue_job(conn, receipt.job_id)
            await conn.execute(
                "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                (worker_id, receipt.job_id),
            )
            next_observation = await inspect_running_claim(
                connection=conn,
                event_id=receipt.event_id,
                job_id=receipt.job_id,
                expected_task_name="b3s_issue_read_noop",
                worker_id=worker_id,
                attempts=0,
            )
            assert next_observation.started_event_id is not None
            newer_start = next_observation.started_event_id
            assert newer_start > started
            newer_claim = await record_claim_generation(
                connection=conn,
                event_id=receipt.event_id,
                job_id=receipt.job_id,
                expected_task_name="b3s_issue_read_noop",
                worker_id=worker_id,
                attempts=0,
                started_event_id=newer_start,
            )
            assert newer_claim.generation == claim.generation + 1
            latest_claim = await load_latest_claim_generation(connection=conn, event_id=receipt.event_id)
            assert latest_claim == replace(newer_claim, disposition="existing")
            reads_before_regeneration = api.get_calls
            with pytest.raises(IssueLabelHold, match="claim_or_plan_invalid"):
                await writer.execute(write_intent)
            assert api.get_calls == reads_before_regeneration and api.post_calls == 0
            # Even a fresh binding holding the latest Generation may
            # not execute a write Intent created by the old Generation.
            fresh_writer = bind_issue_add_label_contract(
                intent=write_intent,
                target=target,
                label="triaged",
                source_intent=intent,
                source_checkpoint=checkpoint,
                observe=fake_get,
                add_label=fake_post,
                write_scope=replace(scope, generation=newer_claim.generation, started_event_id=newer_start),
                test_evidence_digest=DIGEST,
            )
            assert fresh_writer.execute is not None
            with pytest.raises(IssueLabelHold, match="write_scope_intent_mismatch"):
                await fresh_writer.execute(write_intent)
            assert api.get_calls == reads_before_regeneration and api.post_calls == 0
            assert (
                await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label") is None
            )
            return

    asyncio.run(run())


def test_label_scope_reassigned() -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            conn = case.conn
            receipt = case.receipt
            worker_id = case.worker_id
            target = case.target
            intent = case.intent
            checkpoint = case.checkpoint
            write_intent = case.write_intent
            scope = case.scope
            fake_get = case.fake_get
            fake_post = case.fake_post
            api = case.api
            successor = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
            successor_row = await successor.fetchone()
            assert successor_row is not None and successor_row[0] != worker_id
            await conn.execute(
                "UPDATE procrastinate_jobs SET worker_id = %s WHERE id = %s",
                (successor_row[0], receipt.job_id),
            )
            stale_writer = bind_issue_add_label_contract(
                intent=write_intent,
                target=target,
                label="triaged",
                source_intent=intent,
                source_checkpoint=checkpoint,
                observe=fake_get,
                add_label=fake_post,
                write_scope=scope,
                test_evidence_digest=DIGEST,
            )
            assert stale_writer.execute is not None
            reads_before_reassignment = api.get_calls
            with pytest.raises(IssueLabelHold, match="claim_or_plan_invalid"):
                await stale_writer.execute(write_intent)
            assert api.get_calls == reads_before_reassignment and api.post_calls == 0
            assert (
                await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label") is None
            )
            return

    asyncio.run(run())


def test_label_scope_abort() -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            conn = case.conn
            receipt = case.receipt
            target = case.target
            intent = case.intent
            checkpoint = case.checkpoint
            write_intent = case.write_intent
            scope = case.scope
            fake_get = case.fake_get
            fake_post = case.fake_post
            api = case.api
            await conn.execute(
                "UPDATE procrastinate_jobs SET abort_requested = true WHERE id = %s",
                (receipt.job_id,),
            )
            abort_writer = bind_issue_add_label_contract(
                intent=write_intent,
                target=target,
                label="triaged",
                source_intent=intent,
                source_checkpoint=checkpoint,
                observe=fake_get,
                add_label=fake_post,
                write_scope=scope,
                test_evidence_digest=DIGEST,
            )
            assert abort_writer.execute is not None
            reads_before_abort = api.get_calls
            with pytest.raises(IssueLabelHold, match="claim_or_plan_invalid"):
                await abort_writer.execute(write_intent)
            assert api.get_calls == reads_before_abort and api.post_calls == 0
            assert (
                await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label") is None
            )
            return

    asyncio.run(run())


def test_label_scope_lock_lost() -> None:
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
            async with await connect(options) as lock_conn:
                await lock_conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

                async def post_after_disconnect(target: VerifiedTarget, label: str) -> None:
                    assert target.repository == "acme/widget" and label == "triaged"
                    await lock_conn.close()
                    api.post_calls += 1
                    api.observed_labels.add(label)

                interrupted_writer = bind_issue_add_label_contract(
                    intent=write_intent,
                    target=target,
                    label="triaged",
                    source_intent=intent,
                    source_checkpoint=checkpoint,
                    observe=fake_get,
                    add_label=post_after_disconnect,
                    write_scope=replace(scope, connection=lock_conn),
                    test_evidence_digest=DIGEST,
                )
                assert interrupted_writer.execute is not None
                with pytest.raises(psycopg.Error):
                    await interrupted_writer.execute(write_intent)
            assert api.post_calls == 1 and api.observed_labels == {"bug", "triaged"}
            attempt_record = await load_operation_attempt(
                connection=conn, event_id=receipt.event_id, operation_id="label"
            )
            assert attempt_record is not None
            assert attempt_record.attempt.origin_generation == claim.generation
            assert (
                await load_operation_checkpoint(connection=conn, event_id=receipt.event_id, operation_id="label")
                is None
            )
            unknown = await inspect_operation_recovery_evidence(
                connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
            )
            assert unknown.status == "hold" and unknown.reason == "outcome_unknown"
            assert unknown.attempt_digest == attempt_record.attempt_digest
            async with await connect(options) as follower:
                await follower.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                async with follower.transaction():
                    cursor = await follower.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", (issue_label_lock_key(target),)
                    )
                    assert await cursor.fetchone() == (True,)  # The Q78 lock was released.
                restarted_writer = bind_issue_add_label_contract(
                    intent=write_intent,
                    target=target,
                    label="triaged",
                    source_intent=intent,
                    source_checkpoint=checkpoint,
                    observe=fake_get,
                    add_label=fake_post,
                    write_scope=replace(scope, connection=follower),
                    test_evidence_digest=DIGEST,
                )
                assert restarted_writer.execute is not None
                reads_before = api.get_calls
                with pytest.raises(IssueLabelHold, match="attempt_already_recorded"):
                    await restarted_writer.execute(write_intent)
                assert api.get_calls == reads_before and api.post_calls == 1
            return

    asyncio.run(run())


def test_label_scope_crash() -> None:
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
            child_code = """
import asyncio
import json
import os
import sys

import psycopg
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import OperationIntent, record_operation_attempt


async def run():
    args = json.loads(sys.stdin.buffer.read())
    async with await psycopg.AsyncConnection.connect(
        host="127.0.0.1", port=args["port"], dbname="b3s_dev",
        user="b3s_dev", password=args["password"], autocommit=True,
        connect_timeout=4,
    ) as connection:
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(args["schema"])))
        intent = OperationIntent.from_canonical_bytes(bytes.fromhex(args["intent_hex"]))
        await record_operation_attempt(
            connection=connection, event_id=args["event_id"],
            origin_generation=args["generation"], intent=intent,
        )
        os._exit(77)


asyncio.run(run())
"""
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                child_code,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    child.communicate(
                        json.dumps(
                            {
                                "port": options.port,
                                "password": options.password,
                                "schema": schema,
                                "event_id": receipt.event_id,
                                "generation": claim.generation,
                                "intent_hex": write_intent.canonical_bytes().hex(),
                            }
                        ).encode()
                    ),
                    timeout=15,
                )
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert child.returncode == 77, stderr.decode(errors="replace")
            assert stdout == b""
            crashed_attempt = await load_operation_attempt(
                connection=conn, event_id=receipt.event_id, operation_id="label"
            )
            assert crashed_attempt is not None
            assert crashed_attempt.attempt.origin_generation == claim.generation
            assert crashed_attempt.attempt.intent_digest == write_intent.intent_digest
            assert (
                await load_operation_checkpoint(connection=conn, event_id=receipt.event_id, operation_id="label")
                is None
            )
            recovery = await inspect_operation_recovery_evidence(
                connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
            )
            assert recovery.status == "hold" and recovery.reason == "outcome_unknown"
            assert recovery.attempt_digest == crashed_attempt.attempt_digest
            async with await connect(options) as fresh_conn:
                await fresh_conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                restarted_scope = replace(scope, connection=fresh_conn)
                with pytest.raises(IssueLabelHold, match="attempt_already_recorded"):
                    async with restarted_scope(write_intent):
                        pytest.fail("A restarted worker must not enter the POST interval")
                restarted_writer = bind_issue_add_label_contract(
                    intent=write_intent,
                    target=target,
                    label="triaged",
                    source_intent=intent,
                    source_checkpoint=checkpoint,
                    observe=fake_get,
                    add_label=fake_post,
                    write_scope=restarted_scope,
                    test_evidence_digest=DIGEST,
                )
                assert restarted_writer.execute is not None
                get_before = api.get_calls
                with pytest.raises(IssueLabelHold, match="attempt_already_recorded"):
                    await restarted_writer.execute(write_intent)
                assert api.get_calls == get_before  # Scope fences even the pre-POST GET.
            assert api.post_calls == 0 and api.observed_labels == {"bug"}
            return

    asyncio.run(run())


def test_label_scope_normal() -> None:
    async def run() -> None:
        async with prepared_label_scope() as case:
            options = case.options
            schema = case.schema
            conn = case.conn
            attempt_conn = case.attempt_conn
            plan = case.plan
            receipt = case.receipt
            worker_id = case.worker_id
            started = case.started
            claim = case.claim
            checkpoint = case.checkpoint
            write_intent = case.write_intent
            scope = case.scope
            writer = case.writer
            catalog = case.catalog
            api = case.api
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            # Missing Attempt DDL must fail closed, not infer "never POSTed".
            writer, catalog = await assert_attempt_failure_is_atomic(case)
            assert writer.execute is not None and writer.decode is not None and writer.verify is not None
            assert writer.concurrency.guard is not None
            write_raw = await writer.execute(write_intent)
            assert type(write_raw) is bytes
            write_checkpoint = writer.decode(write_raw)
            assert await writer.verify(write_intent, write_checkpoint)
            assert api.post_calls == 1 and api.observed_labels == {"bug", "triaged"}
            attempt_record = await load_operation_attempt(
                connection=conn, event_id=receipt.event_id, operation_id="label"
            )
            assert attempt_record is not None
            assert attempt_record.attempt.origin_generation == claim.generation
            assert attempt_record.attempt.intent_digest == write_intent.intent_digest
            with pytest.raises(AttemptHold, match="already exists"):
                assert attempt_conn is not None
                await record_operation_attempt(
                    connection=attempt_conn,
                    event_id=receipt.event_id,
                    origin_generation=claim.generation,
                    intent=write_intent,
                )
            # A fresh PostgreSQL session must reject a committed Attempt.
            async with await connect(options) as restarted_conn:
                await restarted_conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                restarted_scope = replace(scope, connection=restarted_conn)
                with pytest.raises(IssueLabelHold, match="attempt_already_recorded"):
                    async with restarted_scope(write_intent):
                        pytest.fail("A new DB session must not replay a committed Attempt")
            # Also verify durable replay rejection from an independent
            # interpreter, not merely from another connection in this process.
            # Test credentials travel over stdin, never argv or child stdout.
            child_code = """
import asyncio
import json
import sys

import psycopg
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    AttemptHold,
    OperationIntent,
    load_operation_attempt,
    record_operation_attempt,
)

async def run():
    args = json.loads(sys.stdin.buffer.read())
    async with await psycopg.AsyncConnection.connect(
        host="127.0.0.1", port=args["port"], dbname="b3s_dev",
        user="b3s_dev", password=args["password"], autocommit=True,
        connect_timeout=4,
    ) as connection:
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(args["schema"])))
        intent = OperationIntent.from_canonical_bytes(bytes.fromhex(args["intent_hex"]))
        observed = await load_operation_attempt(
            connection=connection, event_id=args["event_id"], operation_id="label"
        )
        assert observed is not None and observed.attempt_digest == args["attempt_digest"]
        try:
            await record_operation_attempt(
                connection=connection, event_id=args["event_id"],
                origin_generation=args["generation"], intent=intent,
            )
        except AttemptHold as error:
            assert "already exists" in str(error)
            print("attempt_blocked")
        else:
            raise AssertionError("A restarted process must not persist a second Attempt")


asyncio.run(run())
"""
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                child_code,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    child.communicate(
                        json.dumps(
                            {
                                "port": options.port,
                                "password": options.password,
                                "schema": schema,
                                "event_id": receipt.event_id,
                                "generation": claim.generation,
                                "intent_hex": write_intent.canonical_bytes().hex(),
                                "attempt_digest": attempt_record.attempt_digest,
                            }
                        ).encode()
                    ),
                    timeout=15,
                )
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert child.returncode == 0, stderr.decode(errors="replace")
            assert stdout == b"attempt_blocked\n"
            assert api.post_calls == 1
            with pytest.raises(IssueLabelHold, match="attempt_already_recorded"):
                async with replace(scope)(write_intent):
                    pytest.fail("A durable Attempt blocks a second external write")
            unknown = await inspect_operation_recovery_evidence(
                connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
            )
            assert unknown.status == "hold" and unknown.reason == "outcome_unknown"
            assert unknown.attempt_digest == attempt_record.attempt_digest
            # Corrupt only test-owned evidence; re-enable the immutable
            # trigger before observing row integrity to distinguish the
            # invalid record from a missing schema safety constraint.
            await conn.execute(
                "ALTER TABLE github_operation_attempts DISABLE TRIGGER github_operation_attempt_immutable"
            )
            await conn.execute(
                "UPDATE github_operation_attempts SET attempt_digest = %s WHERE event_id = %s AND operation_id = %s",
                (DIGEST, receipt.event_id, "label"),
            )
            await conn.execute(
                "ALTER TABLE github_operation_attempts ENABLE TRIGGER github_operation_attempt_immutable"
            )
            bad = await inspect_operation_recovery_evidence(
                connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
            )
            assert bad.status == "hold" and bad.reason == "attempt_invalid"
            with pytest.raises(IssueLabelHold, match="attempt_corrupted"):
                async with scope(write_intent):
                    pytest.fail("Damaged Attempt must never admit an external write")
            await conn.execute(
                "ALTER TABLE github_operation_attempts DISABLE TRIGGER github_operation_attempt_immutable"
            )
            await conn.execute(
                "UPDATE github_operation_attempts SET attempt_digest = %s WHERE event_id = %s AND operation_id = %s",
                (attempt_record.attempt_digest, receipt.event_id, "label"),
            )
            await conn.execute(
                "ALTER TABLE github_operation_attempts ENABLE TRIGGER github_operation_attempt_immutable"
            )
            assert (
                await record_operation_checkpoint(
                    connection=conn,
                    event_id=receipt.event_id,
                    generation=claim.generation,
                    job_id=receipt.job_id,
                    expected_task_name="b3s_issue_read_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started,
                    expected_plan=plan,
                    checkpoint=write_checkpoint,
                )
            ).disposition == "recorded"
            with pytest.raises(IssueLabelHold, match="checkpoint_already_recorded"):
                async with scope(write_intent):
                    pytest.fail("A committed Checkpoint must block a second write attempt")
            for operation_id, expected_checkpoint in (("read", checkpoint), ("label", write_checkpoint)):
                recovery = await inspect_operation_recovery_evidence(
                    connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id=operation_id
                )
                assert recovery.status == "checkpoint_observed"
                assert recovery.checkpoint_digest == expected_checkpoint.checkpoint_digest
                assert recovery.attempt_digest == (attempt_record.attempt_digest if operation_id == "label" else None)
            assert api.get_calls == 6  # Includes failed Attempt's pre-POST GET; Recovery makes none.
            assert api.post_calls == 1  # Local evidence reads never repeat the remote write.

    asyncio.run(run())
