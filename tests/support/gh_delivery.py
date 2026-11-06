"""Shared GitHub delivery builders and explicit local PostgreSQL test support."""

import hashlib
import hmac
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest
from dotenv import dotenv_values
from procrastinate import App
from procrastinate.tasks import Task
from psycopg import sql

from local_dev_mcp.event_core import EventIdentity, EventObservation
from local_dev_mcp.tools.gh.webhook import VerifiedDelivery, VerifiedTarget
from local_dev_mcp.tools.gh.webhook.receiver import ASGIApp, ASGIMessage

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64


def delivery(
    key: str,
    *,
    digest: str = DIGEST,
    action: str = "opened",
    version: str = "github-webhook:1",
    repo: str = "acme/widget",
    number: int = 123,
    updated: str = "2026-11-02T12:00:00Z",
    kind: str = "issue",
) -> VerifiedDelivery:
    from typing import Literal

    target = VerifiedTarget(
        kind=cast(Literal["issue", "pull_request"], kind),
        repository=repo,
        repository_id=42,
        number=number,
        action=action,
        updated_at=updated,
    )
    event_type = "issues" if kind == "issue" else "pull_request"
    return VerifiedDelivery(
        observation=EventObservation(
            identity=EventIdentity("github:repository:42", key),
            event_type=f"{event_type}.{action}",
            event_schema_version=version,
            observed_at=datetime.now(UTC),
            payload_digest=digest,
        ),
        target=target,
    )


@dataclass(frozen=True, slots=True)
class DbOptions:
    port: int
    password: str


async def connect(options: DbOptions) -> psycopg.AsyncConnection[tuple[Any, ...]]:
    return await psycopg.AsyncConnection.connect(
        host="127.0.0.1",
        port=options.port,
        dbname="b3s_dev",
        user="b3s_dev",
        password=options.password,
        connect_timeout=4,
        autocommit=True,
    )


def connection_options() -> DbOptions:
    env_file = ROOT / "infra" / "postgres" / ".env"
    if not env_file.is_file():
        pytest.skip("Local Podman database not configured")
    config = dotenv_values(env_file)
    password = config.get("B3S_DB_PASSWORD")
    if not isinstance(password, str) or not password:
        pytest.skip("Local test credential not configured")
    port = int(config.get("B3S_DB_PORT") or "55432")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except OSError:
        pytest.skip("Local Podman database not running")
    return DbOptions(port=port, password=password)


async def install_procrastinate_schema(conn: psycopg.AsyncConnection[tuple[Any, ...]]) -> None:
    extension = await conn.execute(
        "SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'plpgsql'"
    )
    assert await extension.fetchone() == ("pg_catalog",)
    bundled = files("procrastinate").joinpath("sql", "schema.sql").read_text(encoding="utf-8")
    await conn.execute(sql.SQL(cast(LiteralString, bundled)))


async def asgi_request(app: ASGIApp, *, raw: bytes, key: str, valid_signature: bool = True) -> int:
    signature = "sha256=" + hmac.new(b"receiver-integration-secret-0123456789", raw, hashlib.sha256).hexdigest()
    if not valid_signature:
        signature = "sha256=" + "0" * 64
    scope: dict[str, object] = {
        "type": "http",
        "path": "/webhooks/github",
        "method": "POST",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
            (b"x-github-event", b"issues"),
            (b"x-github-delivery", key.encode()),
            (b"x-hub-signature-256", signature.encode()),
        ],
    }
    chunks = [raw[: len(raw) // 2], raw[len(raw) // 2 :]]
    sent: list[ASGIMessage] = []
    index = 0

    async def receive() -> ASGIMessage:
        nonlocal index
        assert index < len(chunks)
        chunk = chunks[index]
        index += 1
        return {"type": "http.request", "body": chunk, "more_body": index < len(chunks)}

    async def send(message: ASGIMessage) -> None:
        sent.append(message)

    await app(scope, receive, send)
    assert len(sent) == 2 and sent[0]["type"] == "http.response.start"
    assert sent[1]["type"] == "http.response.body"
    return cast(int, sent[0]["status"])


def register_noop(app: App, *, name: str) -> Task[..., Any, Any]:
    """Register the caller's explicit task name without starting a worker."""

    @app.task(name=name)
    def noop(event_id: int) -> None:
        del event_id

    return noop


async def requeue_job(connection: psycopg.AsyncConnection[tuple[Any, ...]], job_id: int) -> None:
    """Apply Procrastinate's todo transition; triggers still own the event history."""
    await connection.execute("UPDATE procrastinate_jobs SET status = 'todo', worker_id = NULL WHERE id = %s", (job_id,))
