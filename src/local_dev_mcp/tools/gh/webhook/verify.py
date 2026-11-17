"""Bounded, pure GitHub webhook verification without starting an HTTP receiver."""

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from local_dev_mcp.event_core import EventIdentity, EventObservation

MAX_BODY_BYTES = 1024 * 1024
_REQUIRED_HEADERS = frozenset(
    ("content-type", "content-length", "x-github-delivery", "x-github-event", "x-hub-signature-256")
)
_ISSUE_ACTIONS = frozenset(
    ("opened", "edited", "closed", "reopened", "transferred", "labeled", "unlabeled", "assigned", "unassigned")
)
_PR_ACTIONS = frozenset(
    ("opened", "edited", "closed", "reopened", "synchronize", "ready_for_review", "converted_to_draft")
)


class InvalidWebhook(ValueError):
    """A deliberately nonsensitive rejection reason."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VerifiedTarget:
    kind: Literal["issue", "pull_request"]
    repository: str
    repository_id: int
    number: int
    action: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class VerifiedDelivery:
    observation: EventObservation
    target: VerifiedTarget | None


def supports_target_action(kind: str, action: str) -> bool:
    """Use the same accepted issue/PR action set at ingress and pre-claim."""
    if kind == "issue":
        return action in _ISSUE_ACTIONS
    if kind == "pull_request":
        return action in _PR_ACTIONS
    return False


def verify_signature(*, secret: bytes, body: bytes, signature: str) -> bool:
    """GitHub HMAC-SHA256 on the original bytes, compared in constant time."""
    if re.fullmatch(r"sha256=[0-9a-f]{64}", signature) is None:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _headers(values: Sequence[tuple[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in values:
        canonical = name.lower()
        if not name or len(name) > 128 or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
            raise InvalidWebhook("invalid_header")
        if canonical in result:
            raise InvalidWebhook("invalid_header")
        result[canonical] = value
    if not _REQUIRED_HEADERS.issubset(result):
        raise InvalidWebhook("invalid_header")
    if result.get("content-encoding") or result.get("transfer-encoding"):
        raise InvalidWebhook("unsupported_encoding")
    return result


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Ambiguous JSON object.")
        result[name] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("Non-finite number.")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidWebhook("invalid_payload")
    return cast(dict[str, object], value)


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 2**31:
        raise InvalidWebhook("invalid_payload")
    return value


def _exact_int(value: object) -> bool:
    return type(value) is int


def validate_repository_scope(repositories: Mapping[str, int]) -> dict[str, tuple[str, int]]:
    """Validate the explicit name/id allowlist, detecting ambiguous aliases."""
    result: dict[str, tuple[str, int]] = {}
    for name, repo_id in repositories.items():
        if (
            type(name) is not str
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) is None
            or not _exact_int(repo_id)
            or not 0 < repo_id < 2**63
            or name.casefold() in result
        ):
            raise ValueError("Explicit GitHub repository scope must be unique and valid.")
        result[name.casefold()] = (name, repo_id)
    if not result:
        raise ValueError("GitHub webhook scope must be explicit.")
    return result


def verify_delivery(
    *,
    secret: bytes,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    repositories: Mapping[str, int],
) -> VerifiedDelivery:
    """Verify and normalize a scoped GitHub delivery before *any* durable ACK.

    The caller must bound the HTTP body while streaming it. A result with
    target=None is authenticated but not eligible for a queue job. This function
    performs no database transaction and sends no HTTP response.
    """
    if not 32 <= len(secret) <= 8192 or b"\x00" in secret:
        raise ValueError("GitHub webhook secret configuration is invalid.")
    allowed = validate_repository_scope(repositories)
    if not body or len(body) > MAX_BODY_BYTES:
        raise InvalidWebhook("body_limit")
    h = _headers(headers)
    length = h["content-length"]
    if re.fullmatch(r"[0-9]{1,8}", length) is None or int(length) != len(body):
        raise InvalidWebhook("invalid_length")
    content_type = h["content-type"].strip().lower()
    if re.fullmatch(r"application/json(?:\s*;\s*charset=utf-8)?", content_type) is None:
        raise InvalidWebhook("invalid_content_type")
    delivery = h["x-github-delivery"]
    event = h["x-github-event"]
    if re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery) is None or re.fullmatch(r"[a-z_]{1,64}", event) is None:
        raise InvalidWebhook("invalid_header")
    if not verify_signature(secret=secret, body=body, signature=h["x-hub-signature-256"]):
        raise InvalidWebhook("invalid_signature")
    try:
        parsed: object = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_reject_constant
        )
        payload = _mapping(parsed)
    except (ValueError, UnicodeError, RecursionError, TypeError) as error:
        raise InvalidWebhook("invalid_payload") from error

    repo = _mapping(payload.get("repository"))
    full_name = repo.get("full_name")
    if not isinstance(full_name, str):
        raise InvalidWebhook("invalid_payload")
    scoped = allowed.get(full_name.casefold())
    if scoped is None or repo.get("id") != scoped[1] or type(repo.get("id")) is not int:
        raise InvalidWebhook("out_of_scope")
    repo_name, repo_id = scoped

    target: VerifiedTarget | None = None
    action = payload.get("action")
    supported: frozenset[str] = (
        _ISSUE_ACTIONS if event == "issues" else _PR_ACTIONS if event == "pull_request" else frozenset()
    )
    if isinstance(action, str) and action in supported:
        item = _mapping(payload.get("issue" if event == "issues" else "pull_request"))
        number = _positive_int(item.get("number"))
        if "number" in payload and _positive_int(payload["number"]) != number:
            raise InvalidWebhook("invalid_payload")
        updated_at = item.get("updated_at")
        if not isinstance(updated_at, str) or not 0 < len(updated_at) <= 64:
            raise InvalidWebhook("invalid_payload")
        target = VerifiedTarget(
            kind="issue" if event == "issues" else "pull_request",
            repository=repo_name,
            repository_id=repo_id,
            number=number,
            action=action,
            updated_at=updated_at,
        )
    event_type = f"{event}.{action}" if isinstance(action, str) and action in supported else event
    evidence = EventObservation(
        identity=EventIdentity(source_instance_id=f"github:repository:{repo_id}", event_key=delivery),
        event_type=event_type,
        event_schema_version="github-webhook:1",
        observed_at=datetime.now(UTC),
        payload_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    return VerifiedDelivery(observation=evidence, target=target)
