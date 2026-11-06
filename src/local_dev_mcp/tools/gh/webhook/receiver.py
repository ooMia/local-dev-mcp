"""Bounded ASGI GitHub ingress: verify before DB, ACK only after top-level commit.

No HTTP server, worker, or GitHub API client starts when importing this module.
The caller explicitly owns DB connections, Task, credentials and server lifecycle.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeAlias, cast

from procrastinate.tasks import Task
from psycopg import AsyncConnection

from local_dev_mcp.tools.gh.delivery import (
    EventConflictError,
    SchemaReadiness,
    accept_event,
    inspect_delivery_schema,
    require_delivery_schema,
)

from .verify import MAX_BODY_BYTES, InvalidWebhook, validate_repository_scope, verify_delivery

ASGIMessage: TypeAlias = dict[str, object]
Receive: TypeAlias = Callable[[], Awaitable[ASGIMessage]]
Send: TypeAlias = Callable[[ASGIMessage], Awaitable[None]]
ConnectionFactory: TypeAlias = Callable[[], Awaitable[AsyncConnection[tuple[Any, ...]]]]
ASGIApp: TypeAlias = Callable[[Mapping[str, object], Receive, Send], Awaitable[None]]

# Responses are fixed; no credentials, payloads, tracebacks or storage exceptions.
_RESPONSES: dict[str, tuple[int, bytes]] = {
    "accepted": (202, b'{"result":"accepted"}'),
    "duplicate": (200, b'{"result":"duplicate"}'),
    "ignored": (200, b'{"result":"ignored"}'),
    "invalid_header": (400, b'{"result":"invalid_header"}'),
    "invalid_length": (400, b'{"result":"invalid_length"}'),
    "incomplete_body": (400, b'{"result":"incomplete_body"}'),
    "invalid_payload": (400, b'{"result":"invalid_payload"}'),
    "invalid_signature": (401, b'{"result":"invalid_signature"}'),
    "out_of_scope": (403, b'{"result":"out_of_scope"}'),
    "body_limit": (413, b'{"result":"body_limit"}'),
    "invalid_content_type": (415, b'{"result":"invalid_content_type"}'),
    "unsupported_encoding": (400, b'{"result":"unsupported_encoding"}'),
    "conflict": (409, b'{"result":"conflict"}'),
    "storage_unavailable": (503, b'{"result":"storage_unavailable"}'),
    "not_found": (404, b'{"result":"not_found"}'),
    "method_not_allowed": (405, b'{"result":"method_not_allowed"}'),
}


async def _reply(send: Send, code: str) -> None:
    status, body = _RESPONSES[code]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _header_pairs(scope: Mapping[str, object]) -> list[tuple[str, str]]:
    raw = scope.get("headers")
    if not isinstance(raw, (list, tuple)):
        raise InvalidWebhook("invalid_header")
    pairs: list[tuple[str, str]] = []
    for element in cast(Sequence[object], raw):
        if not isinstance(element, (list, tuple)):
            raise InvalidWebhook("invalid_header")
        pair = cast(Sequence[object], element)
        if len(pair) != 2:
            raise InvalidWebhook("invalid_header")
        name, value = pair
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            raise InvalidWebhook("invalid_header")
        try:
            pairs.append((name.decode("ascii"), value.decode("latin1")))
        except UnicodeError as error:
            raise InvalidWebhook("invalid_header") from error
    return pairs


def _advertised_size(pairs: list[tuple[str, str]]) -> int:
    values = [value for name, value in pairs if name.lower() == "content-length"]
    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
        raise InvalidWebhook("invalid_length")
    size = int(values[0]) if len(values[0]) <= 8 else MAX_BODY_BYTES + 1
    if size > MAX_BODY_BYTES:
        raise InvalidWebhook("body_limit")
    if size <= 0:
        raise InvalidWebhook("invalid_length")
    return size


async def _read_body(receive: Receive, advertised: int) -> bytes:
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            while True:
                message = await receive()
                if message.get("type") != "http.request":
                    raise InvalidWebhook("incomplete_body")
                chunk = message.get("body", b"")
                more = message.get("more_body", False)
                if not isinstance(chunk, bytes) or not isinstance(more, bool):
                    raise InvalidWebhook("invalid_payload")
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise InvalidWebhook("body_limit")
                if len(body) > advertised:
                    raise InvalidWebhook("incomplete_body")
                if not more:
                    break
    except TimeoutError as error:
        raise InvalidWebhook("incomplete_body") from error
    if len(body) != advertised:
        raise InvalidWebhook("incomplete_body")
    return bytes(body)


async def _lifespan(receive: Receive, send: Send) -> None:
    while True:
        event = await receive()
        if event.get("type") == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif event.get("type") == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
        else:
            raise ValueError("Unsupported ASGI lifespan event.")


def create_app(
    *,
    secret: bytes,
    repositories: Mapping[str, int],
    connection_factory: ConnectionFactory,
    task: Task[..., Any, Any],
) -> ASGIApp:
    """Compose one webhook endpoint with an explicit DB connection boundary.

    Database connection is created *only after* verification and target routing.
    A 2xx response follows a committed receipt and matching Procrastinate job,
    or a verified but intentionally ignored event requiring no job.
    """
    if not 32 <= len(secret) <= 8192 or b"\x00" in secret:
        raise ValueError("Invalid webhook secret configuration.")
    # Configuration errors are startup failures, not request-time 500s.
    validate_repository_scope(repositories)
    allowed = dict(repositories)

    async def app(scope: Mapping[str, object], receive: Receive, send: Send) -> None:
        if scope.get("type") == "lifespan":
            await _lifespan(receive, send)
            return
        if scope.get("type") != "http":
            raise ValueError("Unsupported ASGI scope type.")
        if scope.get("path") != "/webhooks/github":
            await _reply(send, "not_found")
            return
        if scope.get("method") != "POST":
            await _reply(send, "method_not_allowed")
            return

        try:
            pairs = _header_pairs(scope)
            size = _advertised_size(pairs)
            raw = await _read_body(receive, size)
            verified = verify_delivery(secret=secret, headers=pairs, body=raw, repositories=allowed)
        except InvalidWebhook as error:
            await _reply(send, error.code if error.code in _RESPONSES else "invalid_payload")
            return

        if verified.target is None:
            await _reply(send, "ignored")
            return

        try:
            async with await connection_factory() as connection:
                await require_delivery_schema(connection=connection)
                result = await accept_event(connection=connection, delivery=verified, task=task)
        except EventConflictError:
            await _reply(send, "conflict")
            return
        except Exception:
            # Fail closed on DB/queue failure, uncertain commit or contract mismatch.
            # Cancellation is not swallowed (CancelledError derives from BaseException).
            await _reply(send, "storage_unavailable")
            return
        await _reply(send, result.disposition)

    return app


async def inspect_receiver_database(*, connection_factory: ConnectionFactory) -> SchemaReadiness:
    """Explicit operator preflight; read-only and never starts a server or worker.

    Connection failures propagate to the caller. This check does not change
    ASGI lifespan semantics or install/migrate schema; the HTTP receiver keeps
    checking schema per supported delivery to fail closed after startup.
    """
    async with await connection_factory() as connection:
        return await inspect_delivery_schema(connection=connection)
