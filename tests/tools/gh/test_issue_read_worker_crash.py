"""Worker process death around immutable read Intent and Checkpoint commits.

All scenarios use isolated PostgreSQL and a real Procrastinate Worker in a
child process, with independent recovery observation in the parent.
"""

import asyncio
import json
import sys
from typing import Any
from uuid import uuid4

import procrastinate
import pytest
from psycopg import AsyncConnection, sql

from local_dev_mcp.tools.gh.delivery import (
    ContractCatalog,
    OperationNode,
    accept_event,
    bind_issue_read_contract,
    inspect_operation_recovery_evidence,
    inspect_running_claim,
    load_latest_claim_generation,
    load_operation_checkpoint,
    load_operation_intent,
    load_sealed_plan,
    make_issue_read_intent,
)
from local_dev_mcp.tools.gh.delivery.issue_read_worker import (
    TASK_NAME,
    IssueReadWorkerHold,
    register_issue_read_worker_task,
    run_issue_read_claim,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    CHECKPOINTS_DDL,
    CLAIM_GENERATIONS_DDL,
    INTENTS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget
from tests.support.gh_delivery import connect, connection_options, delivery, install_procrastinate_schema

pytestmark = pytest.mark.integration
EVIDENCE_DIGEST = "sha256:" + "a" * 64

_CHILD = """
import asyncio
import json
import os
import sys

import procrastinate
import psycopg
from psycopg import sql

import local_dev_mcp.tools.gh.delivery.issue_read_worker as read_worker
from local_dev_mcp.tools.gh.delivery import load_operation_checkpoint, load_operation_intent


async def run():
    data = json.loads(sys.stdin.buffer.read())

    async def factory():
        connection = await psycopg.AsyncConnection.connect(
            host="127.0.0.1", port=data["port"], dbname="b3s_dev",
            user="b3s_dev", password=data["password"], autocommit=True,
            connect_timeout=4,
        )
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(data["schema"])))
        return connection

    async def must_not_read(target):
        raise AssertionError("An Intent-boundary crash must never reach GET")

    reads = 0

    async def verified_read(target):
        nonlocal reads
        reads += 1
        assert target.repository == "acme/widget" and target.number == 123
        return json.dumps({
            "id": 200, "number": 123, "state": "open",
            "updated_at": "2026-11-03T10:00:00Z",
            "repository_url": "https://api.github.com/repos/acme/widget",
            "labels": [{"name": "bug"}],
        }).encode()

    if data["boundary"] == "before":
        async def crash_before_intent(**kwargs):
            os._exit(76)
        read_worker.record_operation_intent = crash_before_intent
    elif data["boundary"] == "after":
        async def crash_after_intent(**kwargs):
            # Independent connection proves Intent is durable BEFORE GET.
            async with await factory() as observer:
                intent = await load_operation_intent(
                    connection=observer, event_id=data["event_id"], operation_id="read"
                )
                assert intent is not None and intent.disposition == "existing"
            os._exit(77)
        read_worker.inspect_claim_plan = crash_after_intent
    elif data["boundary"] == "checkpoint_before":
        async def crash_before_checkpoint(**kwargs):
            assert reads == 2
            async with await factory() as observer:
                assert await load_operation_checkpoint(
                    connection=observer, event_id=data["event_id"], operation_id="read"
                ) is None
            os._exit(78)
        read_worker.record_operation_checkpoint = crash_before_checkpoint
    elif data["boundary"] == "checkpoint_after":
        original_checkpoint_writer = read_worker.record_operation_checkpoint

        async def crash_after_checkpoint(**kwargs):
            assert reads == 2
            saved = await original_checkpoint_writer(**kwargs)
            assert saved.disposition == "recorded"
            # Checkpoint commit has finished and is visible to another session.
            async with await factory() as observer:
                durable = await load_operation_checkpoint(
                    connection=observer, event_id=data["event_id"], operation_id="read"
                )
                assert durable is not None and durable.checkpoint_digest == saved.checkpoint_digest
            os._exit(79)
        read_worker.record_operation_checkpoint = crash_after_checkpoint
    else:
        raise AssertionError("Unknown fault boundary")

    app = procrastinate.App(connector=procrastinate.PsycopgConnector(kwargs={
        "host": "127.0.0.1", "port": data["port"], "dbname": "b3s_dev",
        "user": "b3s_dev", "password": data["password"],
        "options": "-c search_path=" + data["schema"],
    }))
    transport = must_not_read if data["boundary"] in {"before", "after"} else verified_read
    read_worker.register_issue_read_worker_task(
        app=app, connection_factory=factory, transport=transport,
        test_evidence_digest=data["evidence_digest"], queue="github-read-only",
    )
    async with app.open_async():
        await app.run_worker_async(
            wait=False, queues=["github-read-only"],
            install_signal_handlers=False, listen_notify=False,
        )


asyncio.run(run())
"""


@pytest.mark.parametrize("boundary", ["before", "after", "checkpoint_before", "checkpoint_after"])
def test_read_worker_process_death_at_evidence_commit(boundary: str) -> None:
    options = connection_options()
    schema = "b3s_read_crash_" + uuid4().hex

    async def run() -> None:
        connection_kwargs = {
            "host": "127.0.0.1",
            "port": options.port,
            "dbname": "b3s_dev",
            "user": "b3s_dev",
            "password": options.password,
            "options": "-c search_path=" + schema,
        }
        app = procrastinate.App(connector=procrastinate.PsycopgConnector(kwargs=connection_kwargs))
        get_calls = 0

        async def connection_factory() -> AsyncConnection[tuple[Any, ...]]:
            connection = await connect(options)
            await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

        async def fake_get(target: VerifiedTarget) -> bytes:
            nonlocal get_calls
            get_calls += 1
            raise AssertionError("A crash recovery must not automatically GET")

        task = register_issue_read_worker_task(
            app=app,
            connection_factory=connection_factory,
            transport=fake_get,
            test_evidence_digest=EVIDENCE_DIGEST,
            queue="github-read-only",
        )
        target = delivery("read-worker-crash-" + boundary).target
        assert target is not None
        op = OperationNode("read", "github_issue", "read_current", "read:v1")
        intent = make_issue_read_intent(operation=op, target=target)
        contract = bind_issue_read_contract(
            intent=intent,
            target=target,
            transport=fake_get,
            test_evidence_digest=EVIDENCE_DIGEST,
        )
        catalog = ContractCatalog((contract,))

        async with await connect(options) as connection:
            try:
                await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(connection)
                for ddl in (
                    RECEIPTS_DDL,
                    PLANS_DDL,
                    CLAIM_GENERATIONS_DDL,
                    INTENTS_DDL,
                    CHECKPOINTS_DDL,
                ):
                    await connection.execute(ddl)

                receipt = await accept_event(
                    connection=connection,
                    delivery=delivery("read-worker-crash-" + boundary),
                    task=task,
                )
                assert receipt.disposition == "accepted"
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _CHILD,
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
                                    "boundary": boundary,
                                    "evidence_digest": EVIDENCE_DIGEST,
                                }
                            ).encode()
                        ),
                        timeout=20,
                    )
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
                assert (
                    child.returncode
                    == {
                        "before": 76,
                        "after": 77,
                        "checkpoint_before": 78,
                        "checkpoint_after": 79,
                    }[boundary]
                ), stderr.decode(errors="replace")
                assert stdout == b""

                claim = await load_latest_claim_generation(connection=connection, event_id=receipt.event_id)
                plan = await load_sealed_plan(connection=connection, event_id=receipt.event_id)
                saved_intent = await load_operation_intent(
                    connection=connection,
                    event_id=receipt.event_id,
                    operation_id="read",
                )
                checkpoint = await load_operation_checkpoint(
                    connection=connection,
                    event_id=receipt.event_id,
                    operation_id="read",
                )
                assert claim is not None and claim.generation == 1
                assert plan is not None
                assert get_calls == 0
                assert (saved_intent is not None) == (boundary != "before")
                assert (checkpoint is not None) == (boundary == "checkpoint_after")
                if checkpoint is not None:
                    assert checkpoint.recorded_generation == claim.generation
                    assert checkpoint.intent_digest == intent.intent_digest

                recovery = await inspect_operation_recovery_evidence(
                    connection=connection,
                    catalog=catalog,
                    event_id=receipt.event_id,
                    operation_id="read",
                )
                if boundary == "checkpoint_after":
                    assert checkpoint is not None
                    assert recovery.status == "checkpoint_observed"
                    assert recovery.reason is None
                    assert recovery.intent_digest == intent.intent_digest
                    assert recovery.checkpoint_digest == checkpoint.checkpoint_digest
                else:
                    assert recovery.status == "hold"
                    assert recovery.checkpoint_digest is None
                    if boundary == "before":
                        assert recovery.reason == "active_unresolved"
                        assert recovery.intent_digest is None
                    else:
                        assert recovery.reason == "outcome_unknown"
                        assert recovery.intent_digest == intent.intent_digest

                # A fresh Worker instance does not reclaim a still-doing Job
                # after process death. This is NOT a recovery authorization.
                async with app.open_async():
                    await asyncio.wait_for(
                        app.run_worker_async(
                            wait=False,
                            queues=["github-read-only"],
                            install_signal_handlers=False,
                            listen_notify=False,
                        ),
                        timeout=20,
                    )
                assert get_calls == 0

                if boundary != "before":
                    # A test-only new Generation cannot re-execute an old
                    # immutable Intent, even after Checkpoint was committed.
                    worker = await connection.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                    worker_row = await worker.fetchone()
                    assert worker_row is not None
                    new_worker_id = worker_row[0]
                    await connection.execute(
                        "UPDATE procrastinate_jobs SET worker_id = %s WHERE id = %s",
                        (new_worker_id, receipt.job_id),
                    )
                    await connection.execute(
                        "INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'started')",
                        (receipt.job_id,),
                    )
                    current = await inspect_running_claim(
                        connection=connection,
                        event_id=receipt.event_id,
                        job_id=receipt.job_id,
                        expected_task_name=TASK_NAME,
                        worker_id=new_worker_id,
                        attempts=0,
                    )
                    assert current.status == "observed", current.reason
                    with pytest.raises(IssueReadWorkerHold, match="intent_already_recorded"):
                        await run_issue_read_claim(
                            connection_factory=connection_factory,
                            transport=fake_get,
                            test_evidence_digest=EVIDENCE_DIGEST,
                            event_id=receipt.event_id,
                            job_id=receipt.job_id,
                            worker_id=new_worker_id,
                            attempts=0,
                        )
                    assert get_calls == 0
                    latest = await load_latest_claim_generation(connection=connection, event_id=receipt.event_id)
                    assert latest is not None and latest.generation == 2
                    persisted = await load_operation_checkpoint(
                        connection=connection,
                        event_id=receipt.event_id,
                        operation_id="read",
                    )
                    assert (persisted is not None) == (boundary == "checkpoint_after")
                    if persisted is not None:
                        assert checkpoint is not None
                        assert persisted.checkpoint_digest == checkpoint.checkpoint_digest
            finally:
                await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
