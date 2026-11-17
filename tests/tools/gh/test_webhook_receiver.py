"""ASGI GitHub admission checks without an HTTP server or database."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any, Never, cast

import pytest

from local_dev_mcp.tools.gh.webhook.receiver import create_app

pytestmark = pytest.mark.unit
SECRET = b"webhook-receiver-test-secret-0123456789abcdef"
REPOS = {"acme/widget": 42}


def make_body() -> bytes:
    return json.dumps(
        {
            "repository": {"full_name": "acme/widget", "id": 42},
            "action": "opened",
            "issue": {"number": 17, "updated_at": "2026-11-02T12:00:00Z"},
        }
    ).encode()


def make_headers(body: bytes, *, valid: bool = True) -> list[tuple[bytes, bytes]]:
    signature = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    if not valid:
        signature = "sha256=" + "0" * 64
    return [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"x-github-delivery", b"uuid-test"),
        (b"x-github-event", b"issues"),
        (b"x-hub-signature-256", signature.encode()),
    ]


async def call(
    app: Callable[..., Awaitable[None]],
    *,
    body: bytes,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes] | None = None,
    path: str = "/webhooks/github",
    method: str = "POST",
    disconnect: bool = False,
) -> tuple[int, bytes]:
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "headers": headers,
    }
    segments = chunks if chunks is not None else [body]
    sent: list[dict[str, object]] = []
    index = 0

    async def receive() -> dict[str, object]:
        nonlocal index
        if disconnect:
            return {"type": "http.disconnect"}
        if index >= len(segments):
            raise AssertionError("Receiver unexpectedly read beyond request end.")
        chunk = segments[index]
        index += 1
        return {"type": "http.request", "body": chunk, "more_body": index < len(segments)}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    assert len(sent) == 2, sent
    assert sent[0]["type"] == "http.response.start"
    assert sent[1]["type"] == "http.response.body"
    return cast(int, sent[0]["status"]), cast(bytes, sent[1]["body"])


def test_rejected_webhook_never_opens_database() -> None:
    async def run() -> None:
        calls = 0

        async def forbidden_connection() -> Never:
            nonlocal calls
            calls += 1
            raise AssertionError("Database must not be touched before verification.")

        app = create_app(
            secret=SECRET, repositories=REPOS, connection_factory=forbidden_connection, task=cast(Any, None)
        )
        raw = make_body()
        bad = make_headers(raw, valid=False)
        status, _ = await call(app, body=raw, headers=bad)
        assert status == 401
        assert calls == 0

        status, _ = await call(app, body=raw, headers=[*make_headers(raw), make_headers(raw)[-1]])
        assert status == 400 and calls == 0

        status, _ = await call(app, body=raw, headers=make_headers(raw), chunks=[raw[:10], raw[10:]])
        assert status == 503  # Valid authentication reaches (unavailable) DB, no 2xx.
        assert calls == 1

    asyncio.run(run())


def test_size_limits_and_connection_abort_fail_closed() -> None:
    async def never() -> Never:
        raise AssertionError("Database must not be touched")

    async def run() -> None:
        app = create_app(secret=SECRET, repositories=REPOS, connection_factory=never, task=cast(Any, None))
        raw = make_body()
        headers = make_headers(raw)

        huge_headers = [(key, b"99999999") if key == b"content-length" else (key, value) for key, value in headers]
        status, _ = await call(app, body=raw, headers=huge_headers)
        assert status == 413

        status, _ = await call(app, body=raw, headers=headers, chunks=[raw, b"overflow"])
        assert status == 400

        status, _ = await call(app, body=raw, headers=headers, disconnect=True)
        assert status == 400

        status, _ = await call(app, body=raw, headers=headers, method="GET")
        assert status == 405

        status, _ = await call(app, body=raw, headers=headers, path="/other")
        assert status == 404

    asyncio.run(run())


def test_ping_does_not_queue_even_when_signed() -> None:
    async def never() -> Never:
        raise AssertionError("Ignored signed ping does not open DB")

    async def run() -> None:
        app = create_app(secret=SECRET, repositories=REPOS, connection_factory=never, task=cast(Any, None))
        raw = b'{"repository":{"full_name":"acme/widget","id":42},"zen":"ok"}'
        headers = [(k, b"ping" if k == b"x-github-event" else v) for k, v in make_headers(raw)]
        status, response = await call(app, body=raw, headers=headers)
        assert status == 200
        assert b"ignored" in response

    asyncio.run(run())


@pytest.mark.parametrize(
    "repositories",
    [
        {},
        {"acme/widget": 0},
        {"acme/widget": -1},
        {"acme/widget": True},
        {"acme/widget": 2**63},
        {"acme/widget": cast(int, "42")},
        {cast(str, 17): 42},
        {"acme widget": 42},
        {"acme/widget": 42, "ACME/WIDGET": 43},
    ],
)
def test_invalid_repository_scope_rejected_at_app_construction(repositories: dict[str, int]) -> None:
    async def forbidden() -> Never:
        raise AssertionError("App construction must not open a database.")

    with pytest.raises(ValueError, match=r"scope|unique"):
        create_app(
            secret=SECRET,
            repositories=repositories,
            connection_factory=forbidden,
            task=cast(Any, None),
        )


def test_repository_scope_is_snapshotted_during_app_construction() -> None:
    async def forbidden() -> Never:
        raise AssertionError("A valid signed ping must not open a database.")

    async def run() -> None:
        repositories = {"acme/widget": 42}
        app = create_app(
            secret=SECRET,
            repositories=repositories,
            connection_factory=forbidden,
            task=cast(Any, None),
        )
        repositories.clear()
        raw = b'{"repository":{"full_name":"acme/widget","id":42},"zen":"ok"}'
        headers = [(k, b"ping" if k == b"x-github-event" else v) for k, v in make_headers(raw)]
        status, response = await call(app, body=raw, headers=headers)
        assert status == 200 and b"ignored" in response

    asyncio.run(run())
