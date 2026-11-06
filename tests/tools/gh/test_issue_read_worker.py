"""Opt-in GitHub Issue read Worker with actual Procrastinate and isolated PG 18."""

import asyncio
import json
from typing import Any
from uuid import uuid4

import procrastinate
import pytest
from psycopg import AsyncConnection, sql

from local_dev_mcp.tools.gh.delivery import (
    accept_event,
    inspect_running_claim,
    load_latest_claim_generation,
    load_operation_checkpoint,
    load_operation_intent,
    load_sealed_plan,
)
from local_dev_mcp.tools.gh.delivery.issue_read_consumer import IssueReadHold
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


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "failed_get",
        "changed_get",
        "pull_request",
        "abort_after_first_get",
        "start_superseded_during_verify",
    ],
)
def test_explicit_readonly_procrastinate_worker(scenario: str) -> None:
    """A real claimed Job uses only GET, with no automatic retry or side effects."""
    options = connection_options()
    schema = "b3s_read_worker_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(
                kwargs={
                    "host": "127.0.0.1",
                    "port": options.port,
                    "dbname": "b3s_dev",
                    "user": "b3s_dev",
                    "password": options.password,
                    "options": "-c search_path=" + schema,
                }
            )
        )

        async def connection_factory() -> AsyncConnection[tuple[Any, ...]]:
            connection = await connect(options)
            await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

        calls = 0

        async def get_issue(target: VerifiedTarget) -> bytes:
            nonlocal calls
            calls += 1
            assert target.repository == "acme/widget" and target.number == 123
            if scenario == "failed_get":
                raise IssueReadHold("simulated_get_failure")
            if scenario == "abort_after_first_get" and calls == 1:
                # Another session requests abort during the first external
                # read. The Worker must not run a second GET or checkpoint.
                async with await connection_factory() as control:
                    await control.execute(
                        "UPDATE procrastinate_jobs SET abort_requested = TRUE WHERE id = %s",
                        (receipt.job_id,),
                    )
            if scenario == "start_superseded_during_verify" and calls == 2:
                # A newer lifecycle event supersedes the start observed
                # before both GETs. Checkpoint's transactional guard rejects.
                async with await connection_factory() as control:
                    await control.execute(
                        "INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'retried')",
                        (receipt.job_id,),
                    )
            updated_at = "2026-11-03T11:00:00Z" if calls == 2 and scenario == "changed_get" else "2026-11-03T10:00:00Z"
            value = {
                "id": 200,
                "number": 123,
                "state": "open",
                "updated_at": updated_at,
                "repository_url": "https://api.github.com/repos/acme/widget",
                "labels": [{"name": "bug"}],
            }
            return json.dumps(value).encode()

        task = register_issue_read_worker_task(
            app=app,
            connection_factory=connection_factory,
            transport=get_issue,
            test_evidence_digest=EVIDENCE_DIGEST,
            queue="github-read-only",
        )
        assert task.name == TASK_NAME

        async with await connect(options) as connection:
            try:
                await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(connection)
                for ddl in (RECEIPTS_DDL, PLANS_DDL, CLAIM_GENERATIONS_DDL, INTENTS_DDL, CHECKPOINTS_DDL):
                    await connection.execute(ddl)
                receipt = await accept_event(
                    connection=connection,
                    delivery=delivery(
                        "worker-read-" + scenario,
                        kind="pull_request" if scenario == "pull_request" else "issue",
                    ),
                    task=task,
                )
                assert receipt.disposition == "accepted"
                queued = await connection.execute(
                    "SELECT task_name, args, queue_name FROM procrastinate_jobs WHERE id = %s", (receipt.job_id,)
                )
                assert await queued.fetchone() == (TASK_NAME, {"event_id": receipt.event_id}, "github-read-only")

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
                    # A failed Job is not silently picked up and retried.
                    await asyncio.wait_for(
                        app.run_worker_async(
                            wait=False,
                            queues=["github-read-only"],
                            install_signal_handlers=False,
                            listen_notify=False,
                        ),
                        timeout=20,
                    )

                state = await connection.execute(
                    "SELECT status::text, attempts FROM procrastinate_jobs WHERE id = %s", (receipt.job_id,)
                )
                job_state = await state.fetchone()
                assert job_state is not None
                assert job_state[0] == ("succeeded" if scenario == "success" else "failed")
                assert job_state[1] == 1  # Procrastinate increments attempts when the Job finishes.

                claim = await load_latest_claim_generation(connection=connection, event_id=receipt.event_id)
                plan = await load_sealed_plan(connection=connection, event_id=receipt.event_id)
                intent = await load_operation_intent(
                    connection=connection, event_id=receipt.event_id, operation_id="read"
                )
                checkpoint = await load_operation_checkpoint(
                    connection=connection, event_id=receipt.event_id, operation_id="read"
                )
                if scenario == "pull_request":
                    assert calls == 0
                    assert claim is None and plan is None and intent is None and checkpoint is None
                else:
                    assert claim is not None and claim.generation == 1
                    assert plan is not None and len(plan.structure_bytes) > 0
                    assert intent is not None and intent.disposition == "existing"
                    if scenario == "success":
                        assert calls == 2
                        assert checkpoint is not None and checkpoint.recorded_generation == 1
                    else:
                        assert calls == (1 if scenario in {"failed_get", "abort_after_first_get"} else 2)
                        assert checkpoint is None

                if scenario == "failed_get":
                    # Test-only manual re-claim after an unknown read outcome:
                    # the existing Intent must fence a second GET.
                    new_worker = await connection.execute(
                        "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
                    )
                    worker_row = await new_worker.fetchone()
                    assert worker_row is not None
                    new_worker_id = worker_row[0]
                    await connection.execute(
                        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                        (new_worker_id, receipt.job_id),
                    )
                    # Manual test-only state transition does not emit a
                    # Procrastinate started event; add the observable event.
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
                        attempts=1,
                    )
                    assert current.status == "observed", current.reason
                    reads_before = calls
                    with pytest.raises(IssueReadWorkerHold, match="intent_already_recorded"):
                        await run_issue_read_claim(
                            connection_factory=connection_factory,
                            transport=get_issue,
                            test_evidence_digest=EVIDENCE_DIGEST,
                            event_id=receipt.event_id,
                            job_id=receipt.job_id,
                            worker_id=new_worker_id,
                            attempts=1,
                        )
                    assert calls == reads_before
                    latest = await load_latest_claim_generation(connection=connection, event_id=receipt.event_id)
                    assert latest is not None and latest.generation == 2
                    assert (
                        await load_operation_checkpoint(
                            connection=connection, event_id=receipt.event_id, operation_id="read"
                        )
                    ) is None
            finally:
                await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
