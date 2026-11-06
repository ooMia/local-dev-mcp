"""Signed ASGI ingress through an explicitly registered GET-only Worker.

No production registration or live GitHub request: one isolated PostgreSQL
schema, actual Procrastinate scheduling, and a deterministic Fake GET.
"""

import asyncio
import json
from uuid import uuid4

import procrastinate
import pytest
from psycopg import AsyncConnection, sql

from local_dev_mcp.tools.gh.delivery import (
    ContractCatalog,
    OperationNode,
    bind_issue_read_contract,
    inspect_operation_recovery_evidence,
    load_operation_checkpoint,
    load_operation_intent,
    load_target,
    make_issue_read_intent,
    register_issue_read_worker_task,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    CHECKPOINTS_DDL,
    CLAIM_GENERATIONS_DDL,
    INTENTS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from local_dev_mcp.tools.gh.webhook.receiver import create_app
from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget
from tests.support.gh_delivery import asgi_request, connect, connection_options, install_procrastinate_schema

pytestmark = pytest.mark.integration
_SECRET = b"receiver-integration-secret-0123456789"
_EVIDENCE_DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize("scenario", ["verified", "failed_get"])
def test_signed_receiver_to_explicit_read_worker(scenario: str) -> None:
    """Auth, deduplication, durable queue, real Claim and recovery inspection."""
    options = connection_options()
    schema = "b3s_asgi_read_" + uuid4().hex

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
        connection_calls = 0

        async def connection_factory() -> AsyncConnection[tuple[object, ...]]:
            nonlocal connection_calls
            connection_calls += 1
            connection = await connect(options)
            await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

        reads = 0

        async def fake_get(target: VerifiedTarget) -> bytes:
            nonlocal reads
            reads += 1
            assert target.kind == "issue"
            assert (target.repository, target.repository_id, target.number) == ("acme/widget", 42, 123)
            if scenario == "failed_get":
                raise RuntimeError("simulated GET failure")
            return json.dumps(
                {
                    "id": 200,
                    "number": 123,
                    "state": "open",
                    "updated_at": "2026-11-03T10:00:00Z",
                    "repository_url": "https://api.github.com/repos/acme/widget",
                    "labels": [{"name": "bug"}],
                }
            ).encode()

        task = register_issue_read_worker_task(
            app=app,
            connection_factory=connection_factory,
            transport=fake_get,
            test_evidence_digest=_EVIDENCE_DIGEST,
            queue="github-read-only",
        )
        receiver = create_app(
            secret=_SECRET,
            repositories={"acme/widget": 42},
            connection_factory=connection_factory,
            task=task,
        )
        raw = json.dumps(
            {
                "repository": {"full_name": "acme/widget", "id": 42},
                "action": "opened",
                "issue": {"number": 123, "updated_at": "2026-11-02T12:00:00Z"},
            }
        ).encode()
        changed = raw.replace(b'"number": 123', b'"number": 124')
        delivery_key = "read-e2e-" + scenario.replace("_", "-")

        async with await connect(options) as observer:
            try:
                await observer.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await observer.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(observer)
                for ddl in (RECEIPTS_DDL, PLANS_DDL, CLAIM_GENERATIONS_DDL, INTENTS_DDL, CHECKPOINTS_DDL):
                    await observer.execute(ddl)

                # Auth failures must not even open a DB connection.
                assert await asgi_request(receiver, raw=raw, key=delivery_key, valid_signature=False) == 401
                assert connection_calls == 0
                counts = await observer.execute(
                    "SELECT (SELECT count(*) FROM github_event_receipts), (SELECT count(*) FROM procrastinate_jobs)"
                )
                assert await counts.fetchone() == (0, 0)

                assert await asgi_request(receiver, raw=raw, key=delivery_key) == 202
                assert await asgi_request(receiver, raw=raw, key=delivery_key) == 200
                assert await asgi_request(receiver, raw=changed, key=delivery_key) == 409
                receipt = await observer.execute(
                    "SELECT event_id, job_id, payload_digest FROM github_event_receipts WHERE event_key = %s",
                    (delivery_key,),
                )
                stored = await receipt.fetchone()
                assert stored is not None
                event_id, job_id, payload_digest = stored
                assert isinstance(payload_digest, str) and payload_digest.startswith("sha256:")
                queued = await observer.execute(
                    "SELECT task_name, args, status::text, queue_name FROM procrastinate_jobs WHERE id = %s",
                    (job_id,),
                )
                assert await queued.fetchone() == (
                    "github_issue_read_current_v1",
                    {"event_id": event_id},
                    "todo",
                    "github-read-only",
                )
                counts = await observer.execute(
                    "SELECT (SELECT count(*) FROM github_event_receipts), (SELECT count(*) FROM procrastinate_jobs)"
                )
                assert await counts.fetchone() == (1, 1)
                assert reads == 0

                async with app.open_async():
                    for _ in range(2):
                        await asyncio.wait_for(
                            app.run_worker_async(
                                wait=False,
                                queues=["github-read-only"],
                                install_signal_handlers=False,
                                listen_notify=False,
                            ),
                            timeout=20,
                        )
                assert reads == (2 if scenario == "verified" else 1)

                job = await observer.execute(
                    "SELECT status::text, attempts FROM procrastinate_jobs WHERE id = %s", (job_id,)
                )
                assert await job.fetchone() == (("succeeded" if scenario == "verified" else "failed"), 1)

                target = await load_target(connection=observer, event_id=event_id)
                assert target is not None
                op = OperationNode("read", "github_issue", "read_current", "read:v1")
                intent = make_issue_read_intent(operation=op, target=target)
                binding = bind_issue_read_contract(
                    intent=intent,
                    target=target,
                    transport=fake_get,
                    test_evidence_digest=_EVIDENCE_DIGEST,
                )
                saved_intent = await load_operation_intent(connection=observer, event_id=event_id, operation_id="read")
                assert saved_intent is not None and saved_intent.intent_digest == intent.intent_digest
                checkpoint = await load_operation_checkpoint(
                    connection=observer, event_id=event_id, operation_id="read"
                )
                result = await inspect_operation_recovery_evidence(
                    connection=observer,
                    catalog=ContractCatalog((binding,)),
                    event_id=event_id,
                    operation_id="read",
                )
                if scenario == "verified":
                    assert checkpoint is not None
                    assert result.status == "checkpoint_observed"
                    assert result.checkpoint_digest == checkpoint.checkpoint_digest
                else:
                    assert checkpoint is None
                    assert result.status == "hold" and result.reason == "outcome_unknown"
            finally:
                await observer.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
