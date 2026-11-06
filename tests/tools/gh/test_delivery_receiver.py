"""ASGI acknowledgement follows the actual PostgreSQL commit boundary."""

import asyncio
import json
from typing import Any
from uuid import uuid4

import procrastinate
import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery.schema import (
    RECEIPTS_DDL,
)
from local_dev_mcp.tools.gh.webhook.receiver import create_app
from tests.support.gh_delivery import (
    asgi_request,
    connect,
    connection_options,
    install_procrastinate_schema,
    register_noop,
)

pytestmark = pytest.mark.integration


def test_http_receiver_ack_tracks_postgres_commit_and_failure() -> None:
    options = connection_options()
    schema = "b3s_http_" + uuid4().hex
    secret = b"receiver-integration-secret-0123456789"

    async def run() -> None:
        procrastinate_app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        no_op = register_noop(procrastinate_app, name="b3s_http_test_noop")

        async def factory() -> psycopg.AsyncConnection[tuple[Any, ...]]:
            connection = await connect(options)
            await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

        app = create_app(secret=secret, repositories={"acme/widget": 42}, connection_factory=factory, task=no_op)
        raw = json.dumps(
            {
                "repository": {"full_name": "acme/widget", "id": 42},
                "action": "opened",
                "issue": {"number": 17, "updated_at": "2026-11-02T12:00:00Z"},
            }
        ).encode()
        changed = raw.replace(b'"number": 17', b'"number": 18')

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                await conn.execute(RECEIPTS_DDL)

                assert await asgi_request(app, raw=raw, key="http-1", valid_signature=False) == 401
                assert await asgi_request(app, raw=raw, key="http-1") == 202
                assert await asgi_request(app, raw=raw, key="http-1") == 200
                assert await asgi_request(app, raw=changed, key="http-1") == 409

                rows = await conn.execute(
                    "SELECT (SELECT count(*) FROM github_event_receipts), (SELECT count(*) FROM procrastinate_jobs)"
                )
                assert await rows.fetchone() == (1, 1)

                await conn.execute(
                    """CREATE FUNCTION fail_receipt_job_link() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.event_key = 'http-2' AND NEW.job_id IS NOT NULL THEN
                            RAISE EXCEPTION 'test-only rejection';
                        END IF;
                        RETURN NEW;
                    END
                    $$ LANGUAGE plpgsql"""
                )
                await conn.execute(
                    "CREATE TRIGGER fail_http_link BEFORE UPDATE OF job_id ON github_event_receipts "
                    "FOR EACH ROW EXECUTE FUNCTION fail_receipt_job_link()"
                )
                assert await asgi_request(app, raw=raw, key="http-2") == 503
                rows = await conn.execute(
                    "SELECT (SELECT count(*) FROM github_event_receipts), (SELECT count(*) FROM procrastinate_jobs)"
                )
                assert await rows.fetchone() == (1, 1)

                # Removing dedup protection must prevent HTTP 2xx even if INSERT
                # would otherwise appear to work. A schema preflight is read-only.
                await conn.execute("ALTER TABLE github_event_receipts DROP CONSTRAINT github_event_identity")
                assert await asgi_request(app, raw=raw, key="http-3") == 503
                rows = await conn.execute(
                    "SELECT (SELECT count(*) FROM github_event_receipts), (SELECT count(*) FROM procrastinate_jobs)"
                )
                assert await rows.fetchone() == (1, 1)
            finally:
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
