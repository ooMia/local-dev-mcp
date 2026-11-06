"""Opt-in add_label:v2 Worker: real Procrastinate claim, isolated PG, fake GET/POST."""

import asyncio
import json
from uuid import uuid4

import procrastinate
import pytest
from psycopg import AsyncConnection, sql

from local_dev_mcp.tools.gh.delivery import (
    accept_event,
    inspect_running_claim,
    issue_label_lock_key,
    load_operation_attempt,
    load_operation_checkpoint,
    load_operation_intent,
    load_operation_lineage,
    load_sealed_plan,
)
from local_dev_mcp.tools.gh.delivery.issue_label_worker import (
    TASK_NAME,
    IssueLabelWorkerHold,
    register_issue_label_worker_task,
    run_issue_label_claim,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    ATTEMPTS_DDL,
    CHECKPOINTS_DDL,
    CLAIM_GENERATIONS_DDL,
    INTENTS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget
from tests.support.gh_delivery import connect, connection_options, delivery, install_procrastinate_schema

pytestmark = pytest.mark.integration
_EVIDENCE = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "scenario",
    (
        "success",
        "already_present",
        "post_response_lost",
        "postcondition_unverified",
        "external_pre_goal",
        "external_post_goal",
        "external_post_remove",
        "external_verify_remove",
        "external_unrelated_label",
        "abort_after_read",
        "pull_request",
        "missing_attempt_store",
        "disabled_attempt_trigger",
        "missing_lineage_store",
    ),
)
def test_explicit_add_label_worker_never_retries_uncertain_effect(scenario: str) -> None:
    """One sealed two-node Job, with a durable Attempt committed before POST."""
    options = connection_options()
    schema = "b3s_label_worker_" + uuid4().hex
    number = 1000 + int(schema[-12:], 16) % (2**31 - 1000)

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

        async def connection_factory() -> AsyncConnection[tuple[object, ...]]:
            connection = await connect(options)
            await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

        observed = {"bug", "triaged"} if scenario == "already_present" else {"bug"}
        gets = 0
        posts = 0
        post_applied = False
        external_events: list[tuple[str, bool]] = []
        receipt_id: int | None = None

        async def outside_actor(target: VerifiedTarget, change: str, *, lock_busy: bool) -> None:
            # A separate task and PostgreSQL session demonstrate the Q78
            # lock's state. The fake GitHub Actor ignores that lock and
            # changes its simulated remote state regardless.
            async with await connection_factory() as outsider:
                cursor = await outsider.execute("SELECT pg_try_advisory_xact_lock(%s)", (issue_label_lock_key(target),))
                assert await cursor.fetchone() == (not lock_busy,)
            if change == "add_goal":
                observed.add("triaged")
            elif change == "remove_goal":
                observed.discard("triaged")
            elif change == "add_unrelated":
                observed.add("documentation")
            else:
                raise AssertionError("Unknown external Actor effect")
            external_events.append((change, lock_busy))

        async def fake_get(target: VerifiedTarget) -> bytes:
            nonlocal gets
            gets += 1
            assert target.kind == "issue"
            assert (target.repository, target.repository_id, target.number) == ("acme/widget", 42, number)
            if scenario == "abort_after_read" and gets == 2:
                # Read's second GET races with a Job cancellation request.
                assert receipt_id is not None
                async with await connection_factory() as interrupter:
                    await interrupter.execute(
                        "UPDATE procrastinate_jobs SET abort_requested = true WHERE id = "
                        "(SELECT job_id FROM github_event_receipts WHERE event_id = %s)",
                        (receipt_id,),
                    )
            if scenario == "external_pre_goal" and gets == 3:
                # A nonparticipant wins the goal before the guarded POST check.
                await asyncio.create_task(outside_actor(target, "add_goal", lock_busy=True))
            if scenario == "external_verify_remove" and gets == 5:
                # The Worker releases Q78 after execute, before fresh verify.
                await asyncio.create_task(outside_actor(target, "remove_goal", lock_busy=False))
            return json.dumps(
                {
                    "id": 9800,
                    "number": number,
                    "repository_url": "https://api.github.com/repos/acme/widget",
                    "state": "open",
                    "updated_at": "2026-11-03T10:00:00Z",
                    "labels": [{"name": item} for item in sorted(observed)],
                },
                sort_keys=True,
            ).encode()

        async def fake_post(target: VerifiedTarget, label: str) -> object:
            nonlocal posts, post_applied
            assert target.number == number and label == "triaged"
            # The independent Attempt commit MUST be visible from an unrelated
            # session while the Q78 transaction-scoped target lock is held.
            async with await connection_factory() as contender:
                attempt = await contender.execute(
                    "SELECT count(*) FROM github_operation_attempts WHERE event_id = %s AND operation_id = 'label'",
                    (receipt_id,),
                )
                assert await attempt.fetchone() == (1,)
                lock = await contender.execute("SELECT pg_try_advisory_xact_lock(%s)", (issue_label_lock_key(target),))
                assert await lock.fetchone() == (False,)
            posts += 1
            if scenario not in {"postcondition_unverified", "external_post_goal"}:
                observed.add(label)
                post_applied = True
            if scenario in {"external_post_goal", "external_post_remove", "external_unrelated_label"}:
                effect = {
                    "external_post_goal": "add_goal",
                    "external_post_remove": "remove_goal",
                    "external_unrelated_label": "add_unrelated",
                }[scenario]
                await asyncio.create_task(outside_actor(target, effect, lock_busy=True))
            if scenario == "post_response_lost":
                raise ConnectionError("fake lost POST response")
            return None

        task = register_issue_label_worker_task(
            app=app,
            connection_factory=connection_factory,
            observe=fake_get,
            add_label=fake_post,
            label="triaged",
            test_evidence_digest=_EVIDENCE,
            queue="github-add-label",
        )
        assert task.name == TASK_NAME and gets == posts == 0

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
                    ATTEMPTS_DDL,
                ):
                    if scenario != "missing_attempt_store" or ddl != ATTEMPTS_DDL:
                        await connection.execute(ddl)
                if scenario == "disabled_attempt_trigger":
                    await connection.execute(
                        "ALTER TABLE github_operation_attempts DISABLE TRIGGER github_operation_attempt_immutable"
                    )
                if scenario == "missing_lineage_store":
                    await connection.execute("DROP TABLE github_operation_lineages")
                receipt = await accept_event(
                    connection=connection,
                    delivery=delivery(
                        "label-worker-" + scenario,
                        kind="pull_request" if scenario == "pull_request" else "issue",
                        number=number,
                    ),
                    task=task,
                )
                receipt_id = receipt.event_id
                job = await connection.execute(
                    "SELECT task_name, args, queue_name FROM procrastinate_jobs WHERE id = %s",
                    (receipt.job_id,),
                )
                assert await job.fetchone() == (TASK_NAME, {"event_id": receipt.event_id}, "github-add-label")

                async with app.open_async():
                    for _ in range(2):
                        await asyncio.wait_for(
                            app.run_worker_async(
                                wait=False,
                                queues=["github-add-label"],
                                install_signal_handlers=False,
                                listen_notify=False,
                            ),
                            timeout=20,
                        )

                status = await connection.execute(
                    "SELECT status::text, attempts FROM procrastinate_jobs WHERE id = %s",
                    (receipt.job_id,),
                )
                expected_success = scenario in {
                    "success",
                    "already_present",
                    "external_pre_goal",
                    "external_post_goal",
                    "external_unrelated_label",
                }
                assert await status.fetchone() == (
                    "succeeded" if expected_success else "failed",
                    1,
                )
                plan = await load_sealed_plan(connection=connection, event_id=receipt.event_id)
                read_intent = await load_operation_intent(
                    connection=connection, event_id=receipt.event_id, operation_id="read"
                )
                label_intent = await load_operation_intent(
                    connection=connection, event_id=receipt.event_id, operation_id="label"
                )
                read_checkpoint = await load_operation_checkpoint(
                    connection=connection, event_id=receipt.event_id, operation_id="read"
                )
                label_checkpoint = await load_operation_checkpoint(
                    connection=connection, event_id=receipt.event_id, operation_id="label"
                )
                attempt = (
                    None
                    if scenario == "missing_attempt_store"
                    else await load_operation_attempt(
                        connection=connection, event_id=receipt.event_id, operation_id="label"
                    )
                )
                if scenario in {
                    "pull_request",
                    "missing_attempt_store",
                    "disabled_attempt_trigger",
                    "missing_lineage_store",
                }:
                    assert plan is None and read_intent is None and label_intent is None
                    assert read_checkpoint is None and label_checkpoint is None and attempt is None
                    assert gets == posts == 0
                elif scenario == "abort_after_read":
                    assert plan is not None and read_intent is not None
                    assert read_checkpoint is None and label_intent is None
                    assert label_checkpoint is None and attempt is None
                    assert gets == 2 and posts == 0
                else:
                    assert plan is not None and read_intent is not None
                    assert len(plan.structure_bytes) > 0
                    assert read_checkpoint is not None and label_intent is not None
                    assert (
                        await load_operation_lineage(
                            connection=connection, event_id=receipt.event_id, operation_id="label"
                        )
                    ) is not None
                    no_post = scenario in {"already_present", "external_pre_goal"}
                    assert (attempt is not None) == (not no_post)
                    assert (label_checkpoint is not None) == expected_success
                    assert posts == (0 if no_post else 1)
                    expected_gets = {
                        "success": 5,
                        "external_post_goal": 5,
                        "external_verify_remove": 5,
                        "external_unrelated_label": 5,
                        "already_present": 4,
                        "postcondition_unverified": 4,
                        "external_pre_goal": 4,
                        "external_post_remove": 4,
                    }.get(scenario, 3)
                    assert gets == expected_gets

                expected_actor = {
                    "external_pre_goal": ("add_goal", True),
                    "external_post_goal": ("add_goal", True),
                    "external_post_remove": ("remove_goal", True),
                    "external_verify_remove": ("remove_goal", False),
                    "external_unrelated_label": ("add_unrelated", True),
                }.get(scenario)
                assert external_events == ([expected_actor] if expected_actor is not None else [])
                if scenario == "external_post_goal":
                    # A satisfied Checkpoint certifies the goal, NOT that our
                    # Fake POST applied it. The external Actor did that.
                    assert not post_applied and "triaged" in observed
                    assert label_checkpoint is not None and attempt is not None
                if scenario in {"external_post_remove", "external_verify_remove"}:
                    assert post_applied and "triaged" not in observed
                    assert label_checkpoint is None and attempt is not None
                if scenario == "external_unrelated_label":
                    assert observed == {"bug", "triaged", "documentation"}
                    assert label_checkpoint is not None and attempt is not None

                if scenario in {"post_response_lost", "external_post_remove", "external_verify_remove"}:
                    # Test-only manual new Claim: immutable root Intent still
                    # forbids any GET/POST regardless of the observed remote
                    # goal, including when an outside Actor removed it.
                    w = await connection.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                    new_worker = await w.fetchone()
                    assert new_worker is not None
                    await connection.execute(
                        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                        (new_worker[0], receipt.job_id),
                    )
                    await connection.execute(
                        "INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'started')",
                        (receipt.job_id,),
                    )
                    active = await inspect_running_claim(
                        connection=connection,
                        event_id=receipt.event_id,
                        job_id=receipt.job_id,
                        worker_id=new_worker[0],
                        expected_task_name=TASK_NAME,
                        attempts=1,
                    )
                    assert active.status == "observed"
                    before = (gets, posts)
                    with pytest.raises(IssueLabelWorkerHold, match="intent_already_recorded"):
                        await run_issue_label_claim(
                            connection_factory=connection_factory,
                            observe=fake_get,
                            add_label=fake_post,
                            label="triaged",
                            test_evidence_digest=_EVIDENCE,
                            event_id=receipt.event_id,
                            job_id=receipt.job_id,
                            worker_id=new_worker[0],
                            attempts=1,
                        )
                    assert (gets, posts) == before
            finally:
                await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
