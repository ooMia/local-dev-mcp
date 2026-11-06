"""PostgreSQL receipt/envelope deduplication, conflicts, rollback and migration."""

import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import procrastinate
import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    EventConflictError,
    ReceiptInvariantError,
    accept_event,
    load_target,
    lookup_receipt,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    ENVELOPE_SCHEMA_VERSION,
    MIGRATE_EMPTY_V1_TO_V2,
    RECEIPTS_DDL,
)
from local_dev_mcp.tools.gh.webhook import VerifiedDelivery, verify_delivery
from tests.support.gh_delivery import (
    DIGEST,
    connect,
    connection_options,
    delivery,
    install_procrastinate_schema,
    register_noop,
)

pytestmark = pytest.mark.integration


def test_normalized_envelope_atomic_deduplication_and_rollback() -> None:
    options = connection_options()
    schema = "b3s_envelope_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        no_op = register_noop(app, name="b3s_envelope_test_noop")

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                await conn.execute(RECEIPTS_DDL)

                first = await accept_event(connection=conn, delivery=delivery("delivery-A"), task=no_op)
                assert first.disposition == "accepted" and first.event_id > 0 and first.job_id > 0

                stored = await conn.execute(
                    """SELECT source_instance_id, event_key, repository, repository_id,
                              target_kind, target_number, action, target_updated_at,
                              envelope_schema_version FROM github_event_receipts"""
                )
                assert await stored.fetchone() == (
                    "github:repository:42",
                    "delivery-A",
                    "acme/widget",
                    42,
                    "issue",
                    123,
                    "opened",
                    "2026-11-02T12:00:00Z",
                    ENVELOPE_SCHEMA_VERSION,
                )
                cols = await conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'github_event_receipts'",
                    (schema,),
                )
                names = {name for (name,) in await cols.fetchall()}
                assert not {"raw_payload", "payload", "webhook_json"} & names
                queued = await conn.execute("SELECT id, args FROM procrastinate_jobs")
                assert await queued.fetchall() == [(first.job_id, {"event_id": first.event_id})]

                repeated = await accept_event(connection=conn, delivery=delivery("delivery-A"), task=no_op)
                assert repeated.disposition == "duplicate"
                assert (repeated.event_id, repeated.job_id) == (first.event_id, first.job_id)

                async with await connect(options) as lookup_conn:
                    await lookup_conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    assert await lookup_receipt(connection=lookup_conn, delivery=delivery("delivery-A")) == repeated
                    assert await lookup_receipt(connection=lookup_conn, delivery=delivery("missing")) is None
                    assert (
                        await load_target(connection=lookup_conn, event_id=first.event_id)
                        == delivery("delivery-A").target
                    )
                    assert await load_target(connection=lookup_conn, event_id=first.event_id + 999999) is None
                    with pytest.raises(ValueError):
                        await load_target(connection=lookup_conn, event_id=0)

                conflicts = (
                    delivery("delivery-A", digest="sha256:" + "b" * 64),
                    delivery("delivery-A", action="closed"),
                    delivery("delivery-A", repo="acme/other"),
                    delivery("delivery-A", number=999),
                    delivery("delivery-A", updated="2026-11-03T12:00:00Z"),
                    delivery("delivery-A", kind="pull_request"),
                )
                for conflicting in conflicts:
                    with pytest.raises(EventConflictError):
                        await accept_event(connection=conn, delivery=conflicting, task=no_op)
                with pytest.raises(ValueError):
                    await accept_event(
                        connection=conn, delivery=delivery("delivery-A", version="github-webhook:2"), task=no_op
                    )

                with pytest.raises(ValueError):
                    await accept_event(
                        connection=conn,
                        delivery=VerifiedDelivery(delivery("missing").observation, None),
                        task=no_op,
                    )
                async with conn.transaction():
                    with pytest.raises(ValueError):
                        await accept_event(connection=conn, delivery=delivery("in-transaction"), task=no_op)

                async with await connect(options) as second:
                    await second.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    a, b = await asyncio.gather(
                        accept_event(connection=conn, delivery=delivery("delivery-B"), task=no_op),
                        accept_event(connection=second, delivery=delivery("delivery-B"), task=no_op),
                    )
                    assert {a.disposition, b.disposition} == {"accepted", "duplicate"}
                    assert (a.event_id, a.job_id) == (b.event_id, b.job_id)

                await conn.execute(
                    """CREATE FUNCTION reject_job_link() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.event_key = 'delivery-C' AND NEW.job_id IS NOT NULL THEN
                            RAISE EXCEPTION 'test-only forced write failure';
                        END IF;
                        RETURN NEW;
                    END
                    $$ LANGUAGE plpgsql"""
                )
                await conn.execute(
                    "CREATE TRIGGER reject_job_link BEFORE UPDATE OF job_id ON github_event_receipts "
                    "FOR EACH ROW EXECUTE FUNCTION reject_job_link()"
                )
                with pytest.raises(psycopg.Error):
                    await accept_event(connection=conn, delivery=delivery("delivery-C"), task=no_op)

                count = await conn.execute(
                    "SELECT (SELECT count(*) FROM github_event_receipts), (SELECT count(*) FROM procrastinate_jobs)"
                )
                assert await count.fetchone() == (2, 2)

                # Legacy/corrupt envelope versions must never be treated as safe duplicates.
                await conn.execute(
                    "UPDATE github_event_receipts SET envelope_schema_version = 'unrecognized' "
                    "WHERE event_key = 'delivery-A'"
                )
                with pytest.raises(ReceiptInvariantError):
                    await lookup_receipt(connection=conn, delivery=delivery("delivery-A"))
                with pytest.raises(ReceiptInvariantError):
                    await load_target(connection=conn, event_id=first.event_id)

                # A signed webhook's original bytes are used only for verification
                # and digest; the normalized target becomes the durable record.
                raw = json.dumps(
                    {
                        "repository": {"full_name": "acme/widget", "id": 42},
                        "action": "opened",
                        "issue": {"number": 456, "updated_at": "2026-11-02T20:00:00Z"},
                    }
                ).encode("utf-8")
                secret = b"webhook-test-only-secret-0123456789abcdef"
                signature = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
                verified = verify_delivery(
                    secret=secret,
                    headers=(
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(raw))),
                        ("X-GitHub-Delivery", "real-verified"),
                        ("X-GitHub-Event", "issues"),
                        ("X-Hub-Signature-256", signature),
                    ),
                    body=raw,
                    repositories={"acme/widget": 42},
                )
                verified_receipt = await accept_event(connection=conn, delivery=verified, task=no_op)
                assert verified_receipt.disposition == "accepted"
                row = await conn.execute(
                    "SELECT target_number, payload_digest FROM github_event_receipts WHERE event_id = %s",
                    (verified_receipt.event_id,),
                )
                assert await row.fetchone() == (456, "sha256:" + hashlib.sha256(raw).hexdigest())
            finally:
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())


def test_legacy_migration_requires_empty_receipt_table() -> None:
    options = connection_options()
    schema = "b3s_migration_" + uuid4().hex
    legacy_ddl = """
    CREATE TABLE github_event_receipts (
        event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        source_instance_id text NOT NULL,
        event_key text NOT NULL,
        event_type text NOT NULL,
        event_schema_version text NOT NULL,
        payload_digest text NOT NULL,
        observed_at timestamptz NOT NULL,
        job_id bigint UNIQUE,
        CONSTRAINT github_event_identity UNIQUE (source_instance_id, event_key)
    )
    """

    async def run() -> None:
        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await conn.execute(legacy_ddl)
                await conn.execute(
                    """INSERT INTO github_event_receipts
                    (source_instance_id, event_key, event_type, event_schema_version,
                     payload_digest, observed_at)
                    VALUES ('github:repository:42', 'legacy', 'issues.opened',
                            'github-webhook:1', %s, CURRENT_TIMESTAMP)""",
                    (DIGEST,),
                )
                with pytest.raises(psycopg.Error):
                    async with conn.transaction():
                        await conn.execute(MIGRATE_EMPTY_V1_TO_V2)
                columns = await conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'github_event_receipts'",
                    (schema,),
                )
                assert "repository" not in {name for (name,) in await columns.fetchall()}
                await conn.execute("DELETE FROM github_event_receipts")
                async with conn.transaction():
                    await conn.execute(MIGRATE_EMPTY_V1_TO_V2)
                columns = await conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'github_event_receipts'",
                    (schema,),
                )
                assert {"repository", "repository_id", "envelope_schema_version"} <= {
                    name for (name,) in await columns.fetchall()
                }
            finally:
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
