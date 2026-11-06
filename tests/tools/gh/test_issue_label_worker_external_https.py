"""Real TLS API and independent HTTPS Actor outside a participating Q78 Worker."""

import asyncio
import http.client
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any
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
)
from local_dev_mcp.tools.gh.delivery.issue_label_consumer import GitHubIssueLabelHttpTransport
from local_dev_mcp.tools.gh.delivery.issue_label_worker import (
    TASK_NAME,
    IssueLabelWorkerHold,
    register_issue_label_worker_task,
    run_issue_label_claim,
)
from local_dev_mcp.tools.gh.delivery.issue_read_consumer import GitHubIssueHttpTransport
from local_dev_mcp.tools.gh.delivery.schema import (
    ATTEMPTS_DDL,
    CHECKPOINTS_DDL,
    CLAIM_GENERATIONS_DDL,
    INTENTS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from tests.support.gh_delivery import connect, connection_options, delivery, install_procrastinate_schema

pytestmark = pytest.mark.integration
_CERT = Path(__file__).with_name("loopback_tls_test_only.pem")
_DIGEST = "sha256:" + "a" * 64

# Test Actor runs in a distinct interpreter and uses its own PostgreSQL
# session, then independently verifies TLS before changing Fake remote state.
_ACTOR = """
import http.client
import json
import os
import ssl
import sys
import psycopg
from psycopg import sql

data = json.loads(sys.stdin.buffer.read())
with psycopg.connect(
    host="127.0.0.1", port=data["db_port"], dbname="b3s_dev",
    user="b3s_dev", password=data["password"], autocommit=True, connect_timeout=4,
) as db:
    db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(data["schema"])))
    lock = db.execute("SELECT pg_try_advisory_xact_lock(%s)", (data["lock_key"],))
    assert lock.fetchone() == (data["lock_free"],)
    row = db.execute(
        "SELECT count(*) FROM github_operation_attempts "
        "WHERE event_id=%s AND operation_id='label'", (data["event_id"],),
    )
    assert row.fetchone() == (data["attempts"],)

context = ssl.create_default_context(cafile=data["cert"])
assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
conn = http.client.HTTPSConnection("127.0.0.1", data["port"], context=context, timeout=5)
try:
    conn.request(
        "PATCH", "/__test__/actor", body=json.dumps({"effect": data["effect"]}).encode(),
        headers={"Content-Type": "application/json", "X-Test-Actor-Pid": str(os.getpid())},
    )
    response = conn.getresponse()
    assert response.status == 200 and response.read() == b"{}"
finally:
    conn.close()
print(os.getpid())
"""


@pytest.mark.parametrize(
    "scenario", ("before_post", "actor_only_goal", "post_remove", "after_verify", "unrelated_label")
)
def test_real_tls_transport_with_independent_external_actor(scenario: str, monkeypatch: pytest.MonkeyPatch) -> None:
    options = connection_options()
    schema = "b3s_actor_https_" + uuid4().hex
    number = 1000 + int(schema[-12:], 16) % (2**31 - 1000)

    async def run() -> None:
        labels = {"bug"}
        gets = 0
        posts = 0
        worker_applied = False
        actor_events: list[tuple[str, int]] = []
        server_errors: list[str] = []
        port = 0
        event_id = 0

        app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(
                kwargs={
                    "host": "127.0.0.1",
                    "port": options.port,
                    "dbname": "b3s_dev",
                    "user": "b3s_dev",
                    "password": options.password,
                    "options": "-c search_path=" + schema,
                },
            ),
        )

        async def factory() -> AsyncConnection[tuple[Any, ...]]:
            db = await connect(options)
            await db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return db

        target = delivery("actor-https-" + scenario, number=number).target
        assert target is not None

        async def actor(effect: str, *, busy: bool, attempts: int) -> None:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _ACTOR,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    child.communicate(
                        json.dumps(
                            {
                                "db_port": options.port,
                                "password": options.password,
                                "schema": schema,
                                "lock_key": issue_label_lock_key(target),
                                "lock_free": not busy,
                                "event_id": event_id,
                                "attempts": attempts,
                                "port": port,
                                "cert": str(_CERT),
                                "effect": effect,
                            }
                        ).encode()
                    ),
                    12,
                )
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert child.returncode == 0, stderr.decode(errors="replace")
            assert actor_events[-1] == (effect, int(stdout.strip()))
            assert int(stdout.strip()) != os.getpid()

        async def respond(writer: asyncio.StreamWriter, body: bytes) -> None:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + b"Content-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal gets, posts, worker_applied
            try:
                assert writer.get_extra_info("ssl_object") is not None
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                lines = header[:-4].split(b"\r\n")
                method, path, version = lines[0].decode("ascii").split()
                assert version == "HTTP/1.1"
                headers: dict[str, str] = {}
                for line in lines[1:]:
                    key, value = line.split(b":", 1)
                    headers[key.decode("ascii").lower()] = value.strip().decode("ascii")
                body = await asyncio.wait_for(reader.readexactly(int(headers.get("content-length", "0"))), 5)

                if method == "PATCH":
                    assert path == "/__test__/actor"
                    effect = json.loads(body)["effect"]
                    pid = int(headers["x-test-actor-pid"])
                    assert pid != os.getpid() and effect in {"add", "remove", "unrelated"}
                    if effect == "add":
                        labels.add("triaged")
                    elif effect == "remove":
                        labels.discard("triaged")
                    else:
                        labels.add("documentation")
                    actor_events.append((effect, pid))
                    await respond(writer, b"{}")
                    return

                issue = f"/repos/acme/widget/issues/{number}"
                assert headers.get("host") == f"127.0.0.1:{port}"
                if method == "GET":
                    assert path == issue and body == b""
                    gets += 1
                    if scenario == "before_post" and gets == 3:
                        await actor("add", busy=True, attempts=0)
                    if scenario == "after_verify" and gets == 5:
                        await actor("remove", busy=False, attempts=1)
                    payload = json.dumps(
                        {
                            "id": 9800,
                            "number": number,
                            "state": "open",
                            "updated_at": "2026-11-03T10:00:00Z",
                            "repository_url": "https://api.github.com/repos/acme/widget",
                            "labels": [{"name": name} for name in sorted(labels)],
                        }
                    ).encode()
                    await respond(writer, payload)
                else:
                    assert method == "POST" and path == issue + "/labels"
                    assert headers.get("authorization") == "Bearer fake-local-token"
                    assert json.loads(body) == {"labels": ["triaged"]}
                    async with await factory() as observer:
                        attempt = await load_operation_attempt(
                            connection=observer, event_id=event_id, operation_id="label"
                        )
                        assert attempt is not None
                    posts += 1
                    if scenario != "actor_only_goal":
                        labels.add("triaged")
                        worker_applied = True
                    effects = {
                        "actor_only_goal": "add",
                        "post_remove": "remove",
                        "unrelated_label": "unrelated",
                    }
                    if scenario in effects:
                        await actor(effects[scenario], busy=True, attempts=1)
                    await respond(writer, b"{}")
            except Exception as error:
                server_errors.append(repr(error))
            finally:
                writer.close()
                await writer.wait_closed()

        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(str(_CERT))
        listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_tls)
        port = listener.sockets[0].getsockname()[1]

        # Redirect the production transport's constructor, not the class
        # itself (http.client uses super(HTTPSConnection, self)).
        original_init = http.client.HTTPSConnection.__init__

        def local_init(self: http.client.HTTPSConnection, host: str, timeout: float = 10) -> None:
            assert host == "api.github.com"
            context = ssl.create_default_context(cafile=str(_CERT))
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            original_init(self, "127.0.0.1", port, timeout=timeout, context=context)

        monkeypatch.setattr(http.client.HTTPSConnection, "__init__", local_init)
        observe = GitHubIssueHttpTransport(token="fake-local-token")
        post = GitHubIssueLabelHttpTransport(token="fake-local-token")
        task = register_issue_label_worker_task(
            app=app,
            connection_factory=factory,
            observe=observe,
            add_label=post,
            label="triaged",
            test_evidence_digest=_DIGEST,
            queue="github-add-label",
        )

        try:
            async with await connect(options) as db:
                try:
                    await db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                    await db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    await install_procrastinate_schema(db)
                    for ddl in (
                        RECEIPTS_DDL,
                        PLANS_DDL,
                        CLAIM_GENERATIONS_DDL,
                        INTENTS_DDL,
                        CHECKPOINTS_DDL,
                        ATTEMPTS_DDL,
                    ):
                        await db.execute(ddl)
                    receipt = await accept_event(
                        connection=db,
                        delivery=delivery("actor-https-" + scenario, number=number),
                        task=task,
                    )
                    event_id = receipt.event_id
                    async with app.open_async():
                        for _ in range(2):
                            await asyncio.wait_for(
                                app.run_worker_async(
                                    wait=False,
                                    queues=["github-add-label"],
                                    install_signal_handlers=False,
                                    listen_notify=False,
                                ),
                                20,
                            )
                    assert not server_errors, server_errors
                    success = scenario not in {"post_remove", "after_verify"}
                    status = await db.execute(
                        "SELECT status::text, attempts FROM procrastinate_jobs WHERE id=%s",
                        (receipt.job_id,),
                    )
                    assert await status.fetchone() == ("succeeded" if success else "failed", 1)
                    attempt = await load_operation_attempt(connection=db, event_id=event_id, operation_id="label")
                    checkpoint = await load_operation_checkpoint(connection=db, event_id=event_id, operation_id="label")
                    assert (attempt is not None) == (scenario != "before_post")
                    assert (checkpoint is not None) == success
                    assert posts == (0 if scenario == "before_post" else 1)
                    assert (
                        gets
                        == {
                            "before_post": 4,
                            "actor_only_goal": 5,
                            "post_remove": 4,
                            "after_verify": 5,
                            "unrelated_label": 5,
                        }[scenario]
                    )
                    assert len(actor_events) == 1
                    assert (
                        actor_events[0][0]
                        == {
                            "before_post": "add",
                            "actor_only_goal": "add",
                            "post_remove": "remove",
                            "after_verify": "remove",
                            "unrelated_label": "unrelated",
                        }[scenario]
                    )
                    if scenario == "actor_only_goal":
                        assert not worker_applied and "triaged" in labels
                    if scenario == "unrelated_label":
                        assert worker_applied and labels == {"bug", "triaged", "documentation"}
                    if scenario in {"post_remove", "after_verify"}:
                        assert worker_applied and "triaged" not in labels
                        w = await db.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                        row = await w.fetchone()
                        assert row is not None
                        await db.execute(
                            "UPDATE procrastinate_jobs SET status='doing', worker_id=%s WHERE id=%s",
                            (row[0], receipt.job_id),
                        )
                        await db.execute(
                            "INSERT INTO procrastinate_events (job_id, type) VALUES (%s, 'started')",
                            (receipt.job_id,),
                        )
                        current = await inspect_running_claim(
                            connection=db,
                            event_id=event_id,
                            job_id=receipt.job_id,
                            worker_id=row[0],
                            expected_task_name=TASK_NAME,
                            attempts=1,
                        )
                        assert current.status == "observed"
                        before = (gets, posts, len(actor_events))
                        with pytest.raises(IssueLabelWorkerHold, match="intent_already_recorded"):
                            await run_issue_label_claim(
                                connection_factory=factory,
                                observe=observe,
                                add_label=post,
                                label="triaged",
                                test_evidence_digest=_DIGEST,
                                event_id=event_id,
                                job_id=receipt.job_id,
                                worker_id=row[0],
                                attempts=1,
                            )
                        assert (gets, posts, len(actor_events)) == before
                finally:
                    await db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        finally:
            listener.close()
            await listener.wait_closed()

    asyncio.run(run())
