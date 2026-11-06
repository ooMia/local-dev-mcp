"""Read-only schema and queued-envelope preflight against PostgreSQL."""

import asyncio
from typing import Any
from uuid import uuid4

import procrastinate
import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    accept_event,
    inspect_delivery_schema,
    inspect_queued_envelope,
    require_delivery_schema,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    RECEIPTS_DDL,
)
from local_dev_mcp.tools.gh.webhook.receiver import inspect_receiver_database
from tests.support.gh_delivery import (
    connect,
    connection_options,
    delivery,
    install_procrastinate_schema,
    register_noop,
)

pytestmark = pytest.mark.integration


def test_read_only_schema_preflight_requires_columns_and_unique_key() -> None:
    options = connection_options()
    schema = "b3s_preflight_" + uuid4().hex

    async def run() -> None:
        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)

                missing = await inspect_delivery_schema(connection=conn)
                assert not missing.ready and "github_event_receipts_missing_or_invalid" in missing.issues

                # An operator can inspect DB compatibility without starting
                # ASGI lifespan, a worker, or executing any SQL migration.
                async def receiver_factory() -> psycopg.AsyncConnection[tuple[Any, ...]]:
                    other = await connect(options)
                    await other.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    return other

                unavailable = await inspect_receiver_database(connection_factory=receiver_factory)
                assert not unavailable.ready and "github_event_receipts_missing_or_invalid" in unavailable.issues
                await conn.execute(RECEIPTS_DDL)
                operator_readiness = await inspect_receiver_database(connection_factory=receiver_factory)
                assert operator_readiness.ready and operator_readiness.issues == ()

                # Actual PostgreSQL session refuses writes, proving the check
                # requires only SELECT and metadata access.
                await conn.execute("SET default_transaction_read_only = on")
                ready = await inspect_delivery_schema(connection=conn)
                assert ready.ready and ready.issues == ()
                await require_delivery_schema(connection=conn)
                await conn.execute("SET default_transaction_read_only = off")

                await conn.execute("ALTER TABLE github_event_receipts DROP CONSTRAINT github_event_identity")
                unsafe = await inspect_delivery_schema(connection=conn)
                assert not unsafe.ready and "receipt_unique_constraint_missing" in unsafe.issues

                await conn.execute("ALTER TABLE github_event_receipts ALTER COLUMN repository TYPE varchar(100)")
                incompatible = await inspect_delivery_schema(connection=conn)
                assert "github_event_receipts_columns_incompatible" in incompatible.issues

                # A missing queue is not admitted even with the receipt table.
                await conn.execute("DROP TABLE procrastinate_jobs CASCADE")
                no_queue = await inspect_delivery_schema(connection=conn)
                assert not no_queue.ready and "procrastinate_jobs_missing_or_invalid" in no_queue.issues
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())


def test_worker_preclaim_observes_job_link_without_executing_work() -> None:
    """No Procrastinate worker, GitHub mutation or Claim; isolated test schema."""
    options = connection_options()
    schema = "b3s_worker_probe_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        no_op = register_noop(app, name="b3s_worker_probe_noop")

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                await conn.execute(RECEIPTS_DDL)

                accepted = await accept_event(connection=conn, delivery=delivery("worker-A"), task=no_op)
                event_id, job_id = accepted.event_id, accepted.job_id
                assert (
                    await inspect_queued_envelope(
                        connection=conn,
                        expected_task_name="b3s_worker_probe_noop",
                        event_id=event_id + 999,
                        job_id=job_id,
                    )
                ).reason == "receipt_missing"

                job_before = await conn.execute(
                    "SELECT status, task_name FROM procrastinate_jobs WHERE id = %s", (job_id,)
                )
                original_state = await job_before.fetchone()
                assert original_state is not None and original_state[1] == "b3s_worker_probe_noop"
                await conn.execute("SET default_transaction_read_only = on")
                inspected = await inspect_queued_envelope(
                    connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=event_id, job_id=job_id
                )
                assert (inspected.status, inspected.reason) == ("observed", None)
                assert (
                    await inspect_queued_envelope(
                        connection=conn,
                        event_id=event_id,
                        job_id=job_id,
                        expected_task_name="unrelated_task",
                    )
                ).reason == "job_mismatch"
                with pytest.raises(ValueError):
                    await inspect_queued_envelope(
                        connection=conn, event_id=event_id, job_id=job_id, expected_task_name=""
                    )
                assert (
                    await inspect_queued_envelope(
                        connection=conn,
                        expected_task_name="b3s_worker_probe_noop",
                        event_id=event_id,
                        job_id=job_id + 100,
                    )
                ).reason == "job_mismatch"
                with pytest.raises(ValueError):
                    await inspect_queued_envelope(
                        connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=True, job_id=job_id
                    )
                with pytest.raises(ValueError):
                    await inspect_queued_envelope(
                        connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=event_id, job_id=-1
                    )
                async with conn.transaction():
                    with pytest.raises(ValueError):
                        await inspect_queued_envelope(
                            connection=conn,
                            expected_task_name="b3s_worker_probe_noop",
                            event_id=event_id,
                            job_id=job_id,
                        )
                job_after = await conn.execute(
                    "SELECT status, task_name FROM procrastinate_jobs WHERE id = %s", (job_id,)
                )
                assert await job_after.fetchone() == original_state
                await conn.execute("SET default_transaction_read_only = off")

                # A committed receipt with no associated Job is never considered inspectable.
                await conn.execute("UPDATE github_event_receipts SET job_id = NULL WHERE event_id = %s", (event_id,))
                assert (
                    await inspect_queued_envelope(
                        connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=event_id, job_id=job_id
                    )
                ).reason == "job_unlinked"
                await conn.execute(
                    "UPDATE github_event_receipts SET job_id = %s WHERE event_id = %s", (job_id, event_id)
                )

                await conn.execute("UPDATE procrastinate_jobs SET task_name = 'wrong_task' WHERE id = %s", (job_id,))
                assert (
                    await inspect_queued_envelope(
                        connection=conn,
                        event_id=event_id,
                        job_id=job_id,
                        expected_task_name="b3s_worker_probe_noop",
                    )
                ).reason == "job_mismatch"
                await conn.execute(
                    "UPDATE procrastinate_jobs SET task_name = 'b3s_worker_probe_noop' WHERE id = %s",
                    (job_id,),
                )

                await conn.execute(
                    "UPDATE procrastinate_jobs SET args = %s WHERE id = %s",
                    ('{"event_id": 999999}', job_id),
                )
                assert (
                    await inspect_queued_envelope(
                        connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=event_id, job_id=job_id
                    )
                ).reason == "job_mismatch"
                await conn.execute(
                    "UPDATE procrastinate_jobs SET args = %s WHERE id = %s",
                    ('{"event_id": ' + str(event_id) + "}", job_id),
                )

                # Unsupported event versions cannot be inferred from an Envelope.
                await conn.execute(
                    "UPDATE github_event_receipts SET event_schema_version = 'github-webhook:future' "
                    "WHERE event_id = %s",
                    (event_id,),
                )
                assert (
                    await inspect_queued_envelope(
                        connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=event_id, job_id=job_id
                    )
                ).reason == "unsupported_contract"
                await conn.execute(
                    "UPDATE github_event_receipts SET event_schema_version = 'github-webhook:1' WHERE event_id = %s",
                    (event_id,),
                )
                await conn.execute(
                    "UPDATE github_event_receipts SET action = 'unsupported', "
                    "event_type = 'issues.unsupported' WHERE event_id = %s",
                    (event_id,),
                )
                assert (
                    await inspect_queued_envelope(
                        connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=event_id, job_id=job_id
                    )
                ).reason == "unsupported_contract"
                await conn.execute(
                    "UPDATE github_event_receipts SET action = 'opened', "
                    "event_type = 'issues.opened' WHERE event_id = %s",
                    (event_id,),
                )
                await conn.execute("DELETE FROM procrastinate_jobs WHERE id = %s", (job_id,))
                assert (
                    await inspect_queued_envelope(
                        connection=conn, expected_task_name="b3s_worker_probe_noop", event_id=event_id, job_id=job_id
                    )
                ).reason == "job_mismatch"

                count = await conn.execute("SELECT count(*) FROM github_event_receipts")
                assert await count.fetchone() == (1,)
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
