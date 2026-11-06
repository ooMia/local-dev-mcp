"""Real Worker crash and uncertain Attempt/POST outcomes; no automatic retry."""

import asyncio
import json
import ssl
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from uuid import uuid4

import procrastinate
import pytest
from psycopg import AsyncConnection, sql

from local_dev_mcp.tools.gh.delivery import (
    ContractCatalog,
    OperationCheckpoint,
    OperationIntent,
    OperationNode,
    PlanStructure,
    accept_event,
    bind_issue_add_label_contract,
    bind_issue_read_contract,
    inspect_operation_recovery_evidence,
    inspect_running_claim,
    load_latest_claim_generation,
    load_operation_attempt,
    load_operation_checkpoint,
    load_operation_intent,
    load_operation_lineage,
    load_sealed_plan,
    make_issue_add_label_intent,
    make_issue_read_intent,
)
from local_dev_mcp.tools.gh.delivery.issue_label_consumer import AttemptPermit, IssueLabelProbe
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
DIGEST = "sha256:" + "a" * 64
# Public, deliberately non-secret localhost credential; never use for production.
TLS_FIXTURE = Path(__file__).with_name("loopback_tls_test_only.pem")

_CHILD = """
import asyncio
import http.client
import json
import os
import ssl
import sys

import procrastinate
import psycopg
from psycopg import sql

import local_dev_mcp.tools.gh.delivery.issue_label_scope as scope_module

from local_dev_mcp.tools.gh.delivery import load_operation_attempt, load_operation_checkpoint, load_operation_intent
from local_dev_mcp.tools.gh.delivery.issue_label_consumer import GitHubIssueLabelHttpTransport
from local_dev_mcp.tools.gh.delivery.issue_label_worker import register_issue_label_worker_task
from local_dev_mcp.tools.gh.delivery.issue_label_scope import issue_label_lock_key


async def run():
    data = json.loads(sys.stdin.buffer.read())

    factory_calls = 0

    async def factory():
        nonlocal factory_calls
        factory_calls += 1
        # The second connection is the dedicated pre-POST Attempt connection.
        # Only this session traverses the test's PostgreSQL TCP fault proxy.
        proxy_boundaries = {
            "commit_wire_lost", "commit_tls_wire_lost", "commit_tls_hostname_rejected",
        }
        use_proxy = data["boundary"] in proxy_boundaries and factory_calls == 2
        tls = use_proxy and data["boundary"].startswith("commit_tls_")
        connection_args = {
            "host": "127.0.0.2" if data["boundary"] == "commit_tls_hostname_rejected" and tls else "127.0.0.1",
            "port": data["proxy_port"] if use_proxy else data["port"],
            "dbname": "b3s_dev", "user": "b3s_dev", "password": data["password"],
            "autocommit": True, "connect_timeout": 4,
            "gssencmode": "disable",
            "sslmode": "verify-full" if tls else ("disable" if use_proxy else "prefer"),
        }
        if tls:
            connection_args["hostaddr"] = "127.0.0.1"
            connection_args["sslrootcert"] = data["tls_fixture_path"]
        conn = await psycopg.AsyncConnection.connect(**connection_args)
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(data["schema"])))
        return conn

    gets = 0

    async def get(target):
        nonlocal gets
        gets += 1
        assert (target.repository, target.repository_id, target.number) == ("acme/widget", 42, data["number"])
        return json.dumps({
            "id": 900, "number": data["number"], "state": "open",
            "updated_at": "2026-11-03T10:00:00Z",
            "repository_url": "https://api.github.com/repos/acme/widget",
            "labels": [{"name": "bug"}],
        }).encode()

    async def assert_prefix():
        assert gets == 3  # Root GET twice; pre-POST goal GET once.
        async with await factory() as observer:
            assert await load_operation_checkpoint(
                connection=observer, event_id=data["event_id"], operation_id="read"
            ) is not None
            assert await load_operation_intent(
                connection=observer, event_id=data["event_id"], operation_id="label"
            ) is not None

    http_listener = None
    if data["boundary"] == "before":
        original_execute = psycopg.AsyncConnection.execute

        async def die_after_uncommitted_insert(self, query, *args, **kwargs):
            cursor = await original_execute(self, query, *args, **kwargs)
            if "INSERT INTO github_operation_attempts" in str(query):
                # The INSERT succeeded within the *uncommitted* Attempt txn.
                own = await original_execute(
                    self,
                    "SELECT count(*) FROM github_operation_attempts "
                    "WHERE event_id = %s AND operation_id = 'label'",
                    (data["event_id"],),
                )
                assert await own.fetchone() == (1,)
                await assert_prefix()
                async with await factory() as observer:
                    assert await load_operation_attempt(
                        connection=observer, event_id=data["event_id"], operation_id="label"
                    ) is None
                os._exit(86)
            return cursor

        psycopg.AsyncConnection.execute = die_after_uncommitted_insert

        async def post(target, label):
            raise AssertionError("POST before Attempt commit")
    elif data["boundary"] == "after":
        async def post(target, label):
            # Entered after commit_attempt() returned, before any Fake POST effect.
            assert label == "triaged"
            await assert_prefix()
            async with await factory() as observer:
                attempt = await load_operation_attempt(
                    connection=observer, event_id=data["event_id"], operation_id="label"
                )
                assert attempt is not None and attempt.attempt.origin_generation == 1
            os._exit(87)
    elif data["boundary"] == "commit_ack_lost":
        # The real top-level transaction commits, but the caller receives
        # a simulated lost acknowledgement. This does not emulate a TCP fault.
        original_record = scope_module.record_operation_attempt

        async def lose_commit_ack(**kwargs):
            await assert_prefix()
            saved = await original_record(**kwargs)
            async with await factory() as observer:
                durable = await load_operation_attempt(
                    connection=observer, event_id=data["event_id"], operation_id="label"
                )
                assert durable is not None and durable.attempt_digest == saved.attempt_digest
            raise psycopg.OperationalError("simulated lost Attempt COMMIT acknowledgement")

        scope_module.record_operation_attempt = lose_commit_ack

        async def post(target, label):
            os._exit(95)  # Regression: POST must never start without a returned permit.
    elif data["boundary"] in {"post_reply_lost_applied", "post_reply_lost_unapplied"}:
        async def post(target, label):
            # Treat this separate committed row as the fake remote server's
            # receipt. Its effect is independent of the Worker scope rollback.
            assert label == "triaged"
            await assert_prefix()
            async with await factory() as observer:
                durable = await load_operation_attempt(
                    connection=observer, event_id=data["event_id"], operation_id="label"
                )
                assert durable is not None
                lock = await observer.execute(
                    "SELECT pg_try_advisory_xact_lock(%s)", (issue_label_lock_key(target),)
                )
                assert await lock.fetchone() == (False,)
                await observer.execute(
                    "INSERT INTO fake_remote_post_receipts (event_id, goal_applied) VALUES (%s, %s)",
                    (data["event_id"], data["boundary"] == "post_reply_lost_applied"),
                )
            raise ConnectionResetError("simulated lost POST response after transmission")
    elif data["boundary"] in {
        "post_wire_applied", "post_wire_unapplied",
        "post_tls_applied", "post_tls_unapplied", "post_tls_untrusted",
    }:
        # The production adapter still requires api.github.com, but the test
        # replaces only its socket constructor within this isolated child.
        # TLS peers use a test-only localhost certificate and checked trust.
        tls_requested = data["boundary"].startswith("post_tls_")

        async def serve_post(reader, writer):
            try:
                if data["boundary"] == "post_tls_untrusted":
                    os._exit(96)  # A rejected TLS certificate must never deliver POST.
                peer_tls = writer.get_extra_info("ssl_object")
                assert (peer_tls is not None) == tls_requested
                if peer_tls is not None:
                    assert peer_tls.version() in {"TLSv1.2", "TLSv1.3"}
                head = await asyncio.wait_for(reader.readuntil(b"\\r\\n\\r\\n"), 3)
                lines = head.split(b"\\r\\n")
                assert lines[0] == (
                    f"POST /repos/acme/widget/issues/{data['number']}/labels HTTP/1.1"
                ).encode()
                headers = dict(
                    line.split(b": ", 1) for line in lines[1:] if b": " in line
                )
                size = int(headers[b"Content-Length"])
                body = await asyncio.wait_for(reader.readexactly(size), 3)
                assert json.loads(body) == {"labels": ["triaged"]}
                assert headers[b"Authorization"] == b"Bearer fake-local-token"
                await assert_prefix()
                async with await factory() as observer:
                    attempt = await load_operation_attempt(
                        connection=observer, event_id=data["event_id"], operation_id="label"
                    )
                    assert attempt is not None
                    await observer.execute(
                        "INSERT INTO fake_remote_post_receipts (event_id, goal_applied) VALUES (%s, %s)",
                        (data["event_id"], data["boundary"] in {"post_wire_applied", "post_tls_applied"}),
                    )
                # Entire POST arrived; close without any HTTP status/headers.
                # http.client will see real TCP EOF instead of an HTTP reply.
            finally:
                writer.close()
                await writer.wait_closed()

        server_tls = None
        if tls_requested:
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.load_cert_chain(certfile=data["tls_fixture_path"])
        http_listener = await asyncio.start_server(
            serve_post, host="127.0.0.1", port=0, ssl=server_tls
        )
        port = http_listener.sockets[0].getsockname()[1]
        if tls_requested:
            # Keep the stdlib HTTPSConnection class intact: its initializer
            # uses super(HTTPSConnection, self), which fails if the global
            # class name is replaced by a function or a subclass.
            original_https_init = http.client.HTTPSConnection.__init__

            def loopback_https_init(self, host, timeout=10):
                assert host == "api.github.com"
                context = (
                    ssl.create_default_context()
                    if data["boundary"] == "post_tls_untrusted"
                    else ssl.create_default_context(cafile=data["tls_fixture_path"])
                )
                assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
                original_https_init(self, "127.0.0.1", port, timeout=timeout, context=context)

            http.client.HTTPSConnection.__init__ = loopback_https_init
        else:
            def loopback_https(host, timeout=10):
                assert host == "api.github.com"
                return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)

            http.client.HTTPSConnection = loopback_https
        post = GitHubIssueLabelHttpTransport(token="fake-local-token")
    elif data["boundary"] in {
        "commit_wire_lost", "commit_tls_wire_lost", "commit_tls_hostname_rejected",
    }:
        async def post(target, label):
            os._exit(95)  # A missing COMMIT acknowledgement must forbid POST.
    else:
        raise AssertionError("invalid fault boundary")

    app = procrastinate.App(connector=procrastinate.PsycopgConnector(kwargs={
        "host": "127.0.0.1", "port": data["port"], "dbname": "b3s_dev",
        "user": "b3s_dev", "password": data["password"],
        "options": "-c search_path=" + data["schema"],
    }))
    register_issue_label_worker_task(
        app=app, connection_factory=factory, observe=get, add_label=post,
        label="triaged", test_evidence_digest=data["digest"], queue="github-add-label",
    )
    try:
        async with app.open_async():
            await app.run_worker_async(
                wait=False, queues=["github-add-label"],
                install_signal_handlers=False, listen_notify=False,
            )
    finally:
        if http_listener is not None:
            http_listener.close()
            await http_listener.wait_closed()


asyncio.run(run())
"""


async def _drop_pg_commit_response(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    database_port: int,
    dropped: asyncio.Event,
    tls_context: ssl.SSLContext | None = None,
    tls_requested: asyncio.Event | None = None,
    tls_ready: asyncio.Event | None = None,
    tls_rejected: asyncio.Event | None = None,
) -> None:
    """Forward isolated PostgreSQL protocol and discard its COMMIT reply.

    In TLS mode the proxy handles PostgreSQL's SSLRequest, terminates TLS
    with the public localhost test certificate, and forwards only decrypted
    protocol bytes to a private, loopback PostgreSQL backend. The production
    DB and other Worker connections are not modified.
    """
    if tls_context is not None:
        request = await asyncio.wait_for(reader.readexactly(8), timeout=5)
        assert request == (8).to_bytes(4, "big") + (80877103).to_bytes(4, "big")
        assert tls_requested is not None and tls_ready is not None
        tls_requested.set()
        writer.write(b"S")
        await writer.drain()
        try:
            await writer.start_tls(tls_context, ssl_handshake_timeout=5)
        except ssl.SSLError, ConnectionError:
            if tls_rejected is None:
                raise
            tls_rejected.set()
            writer.close()
            return
        tls_ready.set()
        assert writer.get_extra_info("ssl_object") is not None

    server_reader, server_writer = await asyncio.open_connection("127.0.0.1", database_port)
    commit_started = asyncio.Event()

    async def client_to_server() -> None:
        tail = b""
        while chunk := await reader.read(65536):
            if b"COMMIT" in tail + chunk:
                commit_started.set()
            tail = (tail + chunk)[-24:]
            server_writer.write(chunk)
            await server_writer.drain()

    async def server_to_client() -> None:
        tail = b""
        while chunk := await server_reader.read(65536):
            if commit_started.is_set():
                # The PostgreSQL backend has executed COMMIT and emitted its
                # wire CommandComplete message. Do not forward any reply.
                if b"COMMIT\x00" in tail + chunk:
                    dropped.set()
                    writer.transport.abort()
                    return
                tail = (tail + chunk)[-24:]
                continue
            writer.write(chunk)
            await writer.drain()

    tasks = {
        asyncio.create_task(client_to_server()),
        asyncio.create_task(server_to_client()),
    }
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        server_writer.close()
        writer.close()
        await asyncio.gather(server_writer.wait_closed(), writer.wait_closed(), return_exceptions=True)


@pytest.mark.parametrize(
    "boundary",
    [
        "before",
        "after",
        "commit_ack_lost",
        "post_reply_lost_applied",
        "post_reply_lost_unapplied",
        "post_wire_applied",
        "post_wire_unapplied",
        "post_tls_applied",
        "post_tls_unapplied",
        "post_tls_untrusted",
        "commit_wire_lost",
        "commit_tls_wire_lost",
        "commit_tls_hostname_rejected",
    ],
)
def test_label_worker_fault_boundaries_hold_without_retry(boundary: str) -> None:
    options = connection_options()
    schema = "b3s_label_crash_" + uuid4().hex
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

        async def factory() -> AsyncConnection[tuple[Any, ...]]:
            conn = await connect(options)
            await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return conn

        gets = 0
        posts = 0

        async def forbidden_get(target: VerifiedTarget) -> bytes:
            nonlocal gets
            gets += 1
            raise AssertionError("Recovery must not GET")

        async def forbidden_post(target: VerifiedTarget, label: str) -> object:
            nonlocal posts
            posts += 1
            raise AssertionError("Recovery must not POST")

        def forbidden_scope(intent: OperationIntent) -> AbstractAsyncContextManager[AttemptPermit]:
            raise AssertionError("Recovery must not acquire write scope")

        task = register_issue_label_worker_task(
            app=app,
            connection_factory=factory,
            observe=forbidden_get,
            add_label=forbidden_post,
            label="triaged",
            test_evidence_digest=DIGEST,
            queue="github-add-label",
        )
        verified = delivery("label-crash-" + boundary, number=number)
        target = verified.target
        assert target is not None

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                for ddl in (
                    RECEIPTS_DDL,
                    PLANS_DDL,
                    CLAIM_GENERATIONS_DDL,
                    INTENTS_DDL,
                    CHECKPOINTS_DDL,
                    ATTEMPTS_DDL,
                ):
                    await conn.execute(ddl)
                # Test-only stand-in for a remote service's independently
                # committed request receipt; never part of the production schema.
                await conn.execute(
                    "CREATE TABLE fake_remote_post_receipts "
                    "(event_id BIGINT PRIMARY KEY, goal_applied BOOLEAN NOT NULL)"
                )
                receipt = await accept_event(connection=conn, delivery=verified, task=task)
                assert receipt.disposition == "accepted"
                ack_dropped = asyncio.Event()
                tls_requested = asyncio.Event()
                tls_ready = asyncio.Event()
                tls_rejected = asyncio.Event()
                pg_listener = None
                pg_tls = boundary.startswith("commit_tls_")
                if boundary in {"commit_wire_lost", "commit_tls_wire_lost", "commit_tls_hostname_rejected"}:
                    server_tls = None
                    if pg_tls:
                        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        server_tls.load_cert_chain(certfile=str(TLS_FIXTURE))

                    async def proxy_request(
                        client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
                    ) -> None:
                        await _drop_pg_commit_response(
                            client_reader,
                            client_writer,
                            options.port,
                            ack_dropped,
                            tls_context=server_tls,
                            tls_requested=tls_requested,
                            tls_ready=tls_ready,
                            tls_rejected=tls_rejected if boundary == "commit_tls_hostname_rejected" else None,
                        )

                    pg_listener = await asyncio.start_server(proxy_request, "127.0.0.1", 0)
                proxy_port = pg_listener.sockets[0].getsockname()[1] if pg_listener is not None else 0

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
                                    "proxy_port": proxy_port,
                                    "password": options.password,
                                    "schema": schema,
                                    "event_id": receipt.event_id,
                                    "number": number,
                                    "boundary": boundary,
                                    "digest": DIGEST,
                                    "tls_fixture_path": str(TLS_FIXTURE.resolve()),
                                }
                            ).encode()
                        ),
                        timeout=20,
                    )
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
                    if pg_listener is not None:
                        pg_listener.close()
                        await pg_listener.wait_closed()
                if boundary in {"commit_wire_lost", "commit_tls_wire_lost"}:
                    assert ack_dropped.is_set(), "Backend COMMIT result was not observed and discarded"
                if pg_tls:
                    assert tls_requested.is_set(), "PostgreSQL SSLRequest must reach the proxy"
                    if boundary == "commit_tls_wire_lost":
                        assert tls_ready.is_set() and not tls_rejected.is_set()
                    else:
                        # The server may complete TLS negotiation before the
                        # client rejects the peer's certificate hostname.
                        assert not ack_dropped.is_set()
                died = boundary in {"before", "after"}
                expected_exit = {"before": 86, "after": 87}.get(boundary, 0)
                assert child.returncode == expected_exit, stderr.decode(errors="replace")
                if boundary == "post_tls_untrusted":
                    assert "SSLCertVerificationError" in stderr.decode(errors="replace")
                if boundary == "commit_tls_hostname_rejected":
                    # libpq translates hostname diagnostics to the OS locale.
                    # The requested mismatched host and psycopg error are stable.
                    diagnostic = stderr.decode(errors="replace")
                    assert "psycopg.OperationalError" in diagnostic and "127.0.0.2" in diagnostic
                assert stdout == b""
                state = await conn.execute(
                    "SELECT status::text, attempts FROM procrastinate_jobs WHERE id = %s", (receipt.job_id,)
                )
                job_attempts = 0 if died else 1
                assert await state.fetchone() == ("doing" if died else "failed", job_attempts)

                claim = await load_latest_claim_generation(connection=conn, event_id=receipt.event_id)
                plan = await load_sealed_plan(connection=conn, event_id=receipt.event_id)
                if boundary == "commit_tls_hostname_rejected":
                    # Both DB connections are opened before the Claim/Plan.
                    # Certificate rejection is a pre-evidence failure, not an
                    # unknown outcome of a POST or an Attempt COMMIT.
                    assert claim is None and plan is None
                    assert (
                        await load_operation_intent(connection=conn, event_id=receipt.event_id, operation_id="read")
                        is None
                    )
                    assert (
                        await load_operation_intent(connection=conn, event_id=receipt.event_id, operation_id="label")
                        is None
                    )
                    assert (
                        await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label")
                        is None
                    )
                    async with app.open_async():
                        await asyncio.wait_for(
                            app.run_worker_async(
                                wait=False,
                                queues=["github-add-label"],
                                install_signal_handlers=False,
                                listen_notify=False,
                            ),
                            timeout=20,
                        )
                    assert (gets, posts) == (0, 0)
                    return

                assert claim is not None and claim.generation == 1 and plan is not None
                read_op = OperationNode("read", "github_issue", "read_current", "read:v1")
                label_op = OperationNode("label", "github_issue", "add_label", "add_label:v2", ("read",))
                assert plan.structure_digest == PlanStructure((read_op, label_op)).structure_digest

                root = await load_operation_intent(connection=conn, event_id=receipt.event_id, operation_id="read")
                label = await load_operation_intent(connection=conn, event_id=receipt.event_id, operation_id="label")
                source = await load_operation_checkpoint(
                    connection=conn, event_id=receipt.event_id, operation_id="read"
                )
                result = await load_operation_checkpoint(
                    connection=conn, event_id=receipt.event_id, operation_id="label"
                )
                lineage = await load_operation_lineage(connection=conn, event_id=receipt.event_id, operation_id="label")
                attempt = await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label")
                assert root is not None and label is not None and source is not None
                assert result is None and lineage is not None
                assert (attempt is not None) == (boundary not in {"before", "commit_tls_hostname_rejected"})
                if attempt is not None:
                    assert attempt.attempt.origin_generation == 1
                    assert attempt.attempt.intent_digest == label.intent_digest

                remote_receipt = await conn.execute(
                    "SELECT goal_applied FROM fake_remote_post_receipts WHERE event_id = %s",
                    (receipt.event_id,),
                )
                observed_remote = await remote_receipt.fetchone()
                if boundary.startswith(("post_reply_lost_", "post_wire_")) or boundary in {
                    "post_tls_applied",
                    "post_tls_unapplied",
                }:
                    assert observed_remote == (
                        boundary in {"post_reply_lost_applied", "post_wire_applied", "post_tls_applied"},
                    ), stderr.decode(errors="replace")
                else:
                    assert observed_remote is None

                root_intent = make_issue_read_intent(operation=read_op, target=target)
                source_checkpoint = OperationCheckpoint.from_canonical_bytes(source.checkpoint_bytes)
                label_intent, _ = make_issue_add_label_intent(
                    operation=label_op,
                    target=target,
                    label="triaged",
                    source_intent=root_intent,
                    source_checkpoint=source_checkpoint,
                )
                assert root_intent.intent_digest == root.intent_digest
                assert label_intent.intent_digest == label.intent_digest
                reader = bind_issue_read_contract(
                    intent=root_intent,
                    target=target,
                    transport=forbidden_get,
                    test_evidence_digest=DIGEST,
                )
                writer = bind_issue_add_label_contract(
                    intent=label_intent,
                    target=target,
                    label="triaged",
                    source_intent=root_intent,
                    source_checkpoint=source_checkpoint,
                    observe=forbidden_get,
                    add_label=forbidden_post,
                    write_scope=forbidden_scope,
                    test_evidence_digest=DIGEST,
                )
                recovery = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=ContractCatalog((reader, writer)),
                    event_id=receipt.event_id,
                    operation_id="label",
                )
                assert recovery.status == "hold" and recovery.reason == "outcome_unknown"
                assert recovery.intent_digest == label_intent.intent_digest
                assert recovery.checkpoint_digest is None
                assert recovery.attempt_digest == (attempt.attempt_digest if attempt else None)

                if boundary.startswith(("post_reply_lost_", "post_wire_")) or boundary in {
                    "post_tls_applied",
                    "post_tls_unapplied",
                }:
                    # Explicit, read-only goal probe after a lost response:
                    # observing a label is NOT permission to retry or to
                    # attribute the earlier mutation to our request.
                    probe_reads = 0

                    async def manual_probe_get(value: VerifiedTarget) -> bytes:
                        nonlocal probe_reads
                        probe_reads += 1
                        assert value == target
                        cursor = await conn.execute(
                            "SELECT goal_applied FROM fake_remote_post_receipts WHERE event_id = %s",
                            (receipt.event_id,),
                        )
                        remote = await cursor.fetchone()
                        assert remote is not None
                        names = ["bug", "triaged"] if remote[0] else ["bug"]
                        return json.dumps(
                            {
                                "id": 900,
                                "number": number,
                                "state": "open",
                                "updated_at": "2026-11-03T10:00:00Z",
                                "repository_url": "https://api.github.com/repos/acme/widget",
                                "labels": [{"name": item} for item in names],
                            }
                        ).encode()

                    probe_binding = bind_issue_add_label_contract(
                        intent=label_intent,
                        target=target,
                        label="triaged",
                        source_intent=root_intent,
                        source_checkpoint=source_checkpoint,
                        observe=manual_probe_get,
                        add_label=forbidden_post,
                        write_scope=forbidden_scope,
                        test_evidence_digest=DIGEST,
                    )
                    assert probe_binding.probe is not None
                    outcome = await probe_binding.probe(label_intent)
                    assert isinstance(outcome, IssueLabelProbe)
                    assert outcome.status == (
                        "goal_satisfied"
                        if boundary in {"post_reply_lost_applied", "post_wire_applied", "post_tls_applied"}
                        else "inconclusive"
                    )
                    assert outcome.mutation_attributed is False
                    assert probe_reads == 1 and (gets, posts) == (0, 0)
                    assert (
                        await load_operation_checkpoint(
                            connection=conn, event_id=receipt.event_id, operation_id="label"
                        )
                        is None
                    )

                # A restarted Worker must not pick up a still-doing or failed Job.
                async with app.open_async():
                    await asyncio.wait_for(
                        app.run_worker_async(
                            wait=False,
                            queues=["github-add-label"],
                            install_signal_handlers=False,
                            listen_notify=False,
                        ),
                        timeout=20,
                    )
                assert (gets, posts) == (0, 0)

                # Test-only new Worker/started event must also fail before GET.
                cursor = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                row = await cursor.fetchone()
                assert row is not None
                # Test-only forced reassignment (never a production retry).
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (row[0], receipt.job_id),
                )
                await conn.execute(
                    "INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'started')",
                    (receipt.job_id,),
                )
                observation = await inspect_running_claim(
                    connection=conn,
                    event_id=receipt.event_id,
                    job_id=receipt.job_id,
                    expected_task_name=TASK_NAME,
                    worker_id=row[0],
                    attempts=job_attempts,
                )
                assert observation.status == "observed"
                with pytest.raises(IssueLabelWorkerHold, match="intent_already_recorded"):
                    await run_issue_label_claim(
                        connection_factory=factory,
                        observe=forbidden_get,
                        add_label=forbidden_post,
                        label="triaged",
                        test_evidence_digest=DIGEST,
                        event_id=receipt.event_id,
                        job_id=receipt.job_id,
                        worker_id=row[0],
                        attempts=job_attempts,
                    )
                assert (gets, posts) == (0, 0)
                unchanged = await load_operation_attempt(
                    connection=conn, event_id=receipt.event_id, operation_id="label"
                )
                assert (unchanged.attempt_digest if unchanged else None) == recovery.attempt_digest
                latest = await load_latest_claim_generation(connection=conn, event_id=receipt.event_id)
                assert latest is not None and latest.generation == 1
            finally:
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
