"""Current claim observations and immutable generation fencing."""

import asyncio
from typing import Any
from uuid import uuid4

import procrastinate
import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    ClaimGenerationHold,
    OperationNode,
    PlanStructure,
    RunningClaimObservation,
    accept_event,
    inspect_claim_plan,
    inspect_running_claim,
    load_latest_claim_generation,
    record_claim_generation,
    seal_plan_structure,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    CLAIM_GENERATIONS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from tests.support.gh_delivery import (
    connect,
    connection_options,
    delivery,
    install_procrastinate_schema,
    register_noop,
    requeue_job,
)

pytestmark = pytest.mark.integration


def test_running_claim_evidence_is_read_only_and_generation_neutral() -> None:
    """Simulate Procrastinate's trigger-owned state in a disposable schema."""
    options = connection_options()
    schema = "b3s_claim_observation_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        no_op = register_noop(app, name="b3s_claim_observation_noop")

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                await conn.execute(RECEIPTS_DDL)
                accepted = await accept_event(connection=conn, delivery=delivery("claim-test"), task=no_op)
                event_id, job_id = accepted.event_id, accepted.job_id

                first_worker = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                worker_row = await first_worker.fetchone()
                assert worker_row is not None and type(worker_row[0]) is int
                worker_id = int(worker_row[0])
                second_worker = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                other_row = await second_worker.fetchone()
                assert other_row is not None and type(other_row[0]) is int
                other_worker_id = int(other_row[0])
                assert worker_id != other_worker_id

                async def probe(
                    *,
                    worker: int = worker_id,
                    tries: int = 0,
                    task_name: str = "b3s_claim_observation_noop",
                ) -> RunningClaimObservation:
                    return await inspect_running_claim(
                        connection=conn,
                        event_id=event_id,
                        job_id=job_id,
                        expected_task_name=task_name,
                        worker_id=worker,
                        attempts=tries,
                    )

                assert (await probe()).reason == "job_not_running"
                assert (
                    await inspect_running_claim(
                        connection=conn,
                        event_id=event_id + 1000,
                        job_id=job_id,
                        expected_task_name="b3s_claim_observation_noop",
                        worker_id=worker_id,
                        attempts=0,
                    )
                ).reason == "receipt_missing"

                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, job_id),
                )
                before = await conn.execute(
                    "SELECT status::text, worker_id, attempts FROM procrastinate_jobs WHERE id = %s",
                    (job_id,),
                )
                state = await before.fetchone()
                await conn.execute("SET default_transaction_read_only = on")
                observed = await probe()
                assert observed.status == "observed" and observed.reason is None
                assert type(observed.started_event_id) is int and observed.started_event_id > 0
                assert (await probe(worker=other_worker_id)).reason == "worker_mismatch"
                assert (await probe(tries=1)).reason == "attempt_mismatch"
                assert (await probe(task_name="unexpected_task")).reason == "job_mismatch"
                with pytest.raises(ValueError):
                    await probe(tries=-1)
                with pytest.raises(ValueError):
                    await probe(worker=True)
                async with conn.transaction():
                    with pytest.raises(ValueError):
                        await probe()
                after = await conn.execute(
                    "SELECT status::text, worker_id, attempts FROM procrastinate_jobs WHERE id = %s",
                    (job_id,),
                )
                assert await after.fetchone() == state
                await conn.execute("SET default_transaction_read_only = off")

                await conn.execute("UPDATE procrastinate_jobs SET abort_requested = true WHERE id = %s", (job_id,))
                assert (await probe()).reason == "abort_requested"
                await conn.execute("UPDATE procrastinate_jobs SET abort_requested = false WHERE id = %s", (job_id,))
                await conn.execute(
                    "UPDATE procrastinate_jobs SET args = %s WHERE id = %s", ('{"event_id":999999}', job_id)
                )
                assert (await probe()).reason == "job_mismatch"
                await conn.execute(
                    "UPDATE procrastinate_jobs SET args = %s WHERE id = %s",
                    ('{"event_id":' + str(event_id) + "}", job_id),
                )

                # Every new todo -> doing transition produces a different
                # durable Procrastinate event, even for the same worker/job.
                await requeue_job(conn, job_id)
                assert (await probe()).reason == "job_not_running"
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, job_id),
                )
                restarted = await probe()
                assert restarted.status == "observed"
                assert restarted.started_event_id is not None
                assert restarted.started_event_id > observed.started_event_id

                # A stale started event cannot authorize an observation if
                # a newer lifecycle event has superseded it.
                fake_later = await conn.execute(
                    "INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'retried') RETURNING id",
                    (job_id,),
                )
                fake_row = await fake_later.fetchone()
                assert fake_row is not None
                later_event_id = int(fake_row[0])
                assert (await probe()).reason == "start_event_missing"
                await conn.execute("DELETE FROM procrastinate_events WHERE id = %s", (later_event_id,))
                assert (await probe()).status == "observed"

                await conn.execute("DELETE FROM procrastinate_events WHERE job_id = %s AND type = 'started'", (job_id,))
                assert (await probe()).reason == "start_event_missing"
                await conn.execute("DELETE FROM procrastinate_workers WHERE id = %s", (worker_id,))
                assert (await probe()).reason == "worker_mismatch"
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())


def test_claim_generation_records_attempts_and_rejects_stale_workers() -> None:
    """Isolated PostgreSQL 18 ledger, never runs a worker or remote API."""
    options = connection_options()
    schema = "b3s_claim_generation_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        noop = register_noop(app, name="b3s_generation_test")

        plan = PlanStructure((OperationNode("read", "github_issue", "read_current", "read:v1"),))
        other_plan = PlanStructure((OperationNode("check", "github_issue", "read_current", "read:v1"),))

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                await conn.execute(RECEIPTS_DDL)
                await conn.execute(CLAIM_GENERATIONS_DDL)
                await conn.execute(PLANS_DDL)
                first = await accept_event(connection=conn, delivery=delivery("claim-gen-1"), task=noop)
                event_id, job_id = first.event_id, first.job_id

                assert await load_latest_claim_generation(connection=conn, event_id=event_id) is None
                with pytest.raises(ClaimGenerationHold):
                    await record_claim_generation(
                        connection=conn,
                        event_id=event_id,
                        job_id=job_id,
                        expected_task_name="b3s_generation_test",
                        worker_id=1,
                        attempts=0,
                        started_event_id=1,
                    )

                worker = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                first_row = await worker.fetchone()
                assert first_row is not None
                first_worker = int(first_row[0])
                other = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                other_row = await other.fetchone()
                assert other_row is not None
                second_worker = int(other_row[0])

                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (first_worker, job_id),
                )
                observed = await inspect_running_claim(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_generation_test",
                    worker_id=first_worker,
                    attempts=0,
                )
                assert observed.status == "observed" and observed.started_event_id is not None
                started = observed.started_event_id

                async def record(
                    connection: psycopg.AsyncConnection[tuple[Any, ...]],
                    *,
                    worker_id: int = first_worker,
                    start_id: int = started,
                ) -> Any:
                    return await record_claim_generation(
                        connection=connection,
                        event_id=event_id,
                        job_id=job_id,
                        expected_task_name="b3s_generation_test",
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=start_id,
                    )

                async with await connect(options) as second:
                    await second.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    a, b = await asyncio.gather(record(conn), record(second))
                    assert {a.disposition, b.disposition} == {"recorded", "existing"}
                    assert (a.generation, b.generation) == (1, 1)
                    assert a.started_event_id == b.started_event_id == started
                    assert (await record(second)).disposition == "existing"

                async def plan_status(
                    *,
                    generation: int = 1,
                    worker_id: int = first_worker,
                    start_id: int = started,
                    expected_plan: PlanStructure = plan,
                ) -> tuple[str, str | None]:
                    found = await inspect_claim_plan(
                        connection=conn,
                        event_id=event_id,
                        generation=generation,
                        job_id=job_id,
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=start_id,
                        expected_task_name="b3s_generation_test",
                        expected_plan=expected_plan,
                    )
                    return found.status, found.reason

                assert await plan_status() == ("hold", "plan_missing")
                await seal_plan_structure(connection=conn, event_id=event_id, plan=plan)
                assert await plan_status() == ("observed", None)
                assert await plan_status(generation=2) == ("hold", "claim_generation_mismatch")
                assert await plan_status(expected_plan=other_plan) == ("hold", "plan_mismatch")

                await conn.execute("SET default_transaction_read_only = on")
                loaded = await load_latest_claim_generation(connection=conn, event_id=event_id)
                assert loaded is not None and loaded.generation == 1
                assert await plan_status() == ("observed", None)
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    await record(conn)
                await conn.execute("SET default_transaction_read_only = off")

                with pytest.raises(ClaimGenerationHold):
                    await record(conn, worker_id=second_worker)
                with pytest.raises(ClaimGenerationHold):
                    await record(conn, start_id=started + 9000)

                # Existing webhook evidence must still be a supported contract.
                await conn.execute(
                    "UPDATE github_event_receipts SET event_schema_version = 'github-webhook:future' "
                    "WHERE event_id = %s",
                    (event_id,),
                )
                with pytest.raises(ClaimGenerationHold):
                    await record(conn)
                assert await plan_status() == ("hold", "unsupported_contract")
                await conn.execute(
                    "UPDATE github_event_receipts SET event_schema_version = 'github-webhook:1' WHERE event_id = %s",
                    (event_id,),
                )
                await conn.execute(
                    "UPDATE github_event_receipts SET target_updated_at = '' WHERE event_id = %s",
                    (event_id,),
                )
                assert await plan_status() == ("hold", "unsupported_contract")
                await conn.execute(
                    "UPDATE github_event_receipts SET target_updated_at = '2026-11-02T12:00:00Z' WHERE event_id = %s",
                    (event_id,),
                )
                assert await plan_status() == ("observed", None)
                count = await conn.execute("SELECT count(*) FROM github_delivery_claim_generations")
                assert await count.fetchone() == (1,)

                # Simulated ownership transfer: a new Procrastinate 'started'
                # event must be bound to a new local Generation.
                await requeue_job(conn, job_id)
                with pytest.raises(ClaimGenerationHold):
                    await record(conn)
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (second_worker, job_id),
                )
                next_observation = await inspect_running_claim(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_generation_test",
                    worker_id=second_worker,
                    attempts=0,
                )
                assert next_observation.status == "observed" and next_observation.started_event_id is not None
                newer_start = next_observation.started_event_id
                assert newer_start > started
                with pytest.raises(ClaimGenerationHold):
                    await record(conn)
                generation2 = await record(conn, worker_id=second_worker, start_id=newer_start)
                assert generation2.generation == 2 and generation2.disposition == "recorded"
                assert (await record(conn, worker_id=second_worker, start_id=newer_start)).disposition == "existing"

                assert await plan_status() == ("hold", "job_not_running")
                assert await plan_status(generation=1, worker_id=second_worker, start_id=newer_start) == (
                    "hold",
                    "claim_generation_mismatch",
                )
                assert await plan_status(generation=2, worker_id=second_worker, start_id=newer_start) == (
                    "observed",
                    None,
                )

                with pytest.raises(ClaimGenerationHold):
                    await record(conn, worker_id=second_worker, start_id=started)
                latest = await load_latest_claim_generation(connection=conn, event_id=event_id)
                assert latest is not None and latest.generation == 2

                history = await conn.execute(
                    "SELECT generation, started_event_id, worker_id FROM github_delivery_claim_generations "
                    "WHERE event_id = %s ORDER BY generation",
                    (event_id,),
                )
                assert await history.fetchall() == [(1, started, first_worker), (2, newer_start, second_worker)]

                for stmt in (
                    "UPDATE github_delivery_claim_generations SET worker_id = %s WHERE event_id = %s",
                    "DELETE FROM github_delivery_claim_generations WHERE event_id = %s",
                ):
                    if stmt.startswith("UPDATE"):
                        args: tuple[int, ...] = (first_worker, event_id)
                    else:
                        args = (event_id,)
                    with pytest.raises(psycopg.Error):
                        await conn.execute(stmt, args)
                await conn.execute("UPDATE procrastinate_jobs SET abort_requested = true WHERE id = %s", (job_id,))
                with pytest.raises(ClaimGenerationHold):
                    await record(conn, worker_id=second_worker, start_id=newer_start)
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
