"""Pure GitHub webhook admission checks; no network, database, or worker."""

import hashlib
import hmac
import json

import pytest

from local_dev_mcp.tools.gh.webhook.verify import (
    MAX_BODY_BYTES,
    InvalidWebhook,
    verify_delivery,
    verify_signature,
)

pytestmark = pytest.mark.unit

SECRET = b"webhook-only-secret-with-high-entropy-0123456789"
REPOSITORIES = {"acme/widget": 42}


def payload(
    *,
    repo_name: str = "acme/widget",
    repo_id: int = 42,
    number: int = 7,
    action: str = "opened",
    kind: str = "issues",
) -> bytes:
    obj: dict[str, object] = {
        "repository": {"full_name": repo_name, "id": repo_id},
        "action": action,
        "number": number,
        "issue" if kind == "issues" else "pull_request": {
            "number": number,
            "updated_at": "2026-11-02T12:00:00Z",
        },
    }
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def headers(raw: bytes, *, event: str = "issues", delivery: str = "uuid-123") -> list[tuple[str, str]]:
    return [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(raw))),
        ("X-GitHub-Delivery", delivery),
        ("X-GitHub-Event", event),
        ("X-Hub-Signature-256", "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()),
    ]


def test_github_documented_hmac_vector() -> None:
    assert verify_signature(
        secret=b"It's a Secret to Everybody",
        body=b"Hello, World!",
        signature="sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17",
    )


def test_verified_issue_keeps_identity_evidence_separate_from_target() -> None:
    raw = payload()
    result = verify_delivery(secret=SECRET, headers=headers(raw), body=raw, repositories=REPOSITORIES)
    assert result.observation.identity.source_instance_id == "github:repository:42"
    assert result.observation.identity.event_key == "uuid-123"
    assert result.observation.payload_digest == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert result.observation.event_type == "issues.opened"
    assert result.observation.event_schema_version == "github-webhook:1"
    assert result.target is not None
    assert (result.target.kind, result.target.repository, result.target.number) == ("issue", "acme/widget", 7)
    assert result.target.action == "opened"
    assert "payload" not in result.__dataclass_fields__
    assert "secret" not in result.__dataclass_fields__


def test_supported_pull_request_event() -> None:
    raw = payload(kind="pull_request", action="synchronize")
    result = verify_delivery(
        secret=SECRET, headers=headers(raw, event="pull_request"), body=raw, repositories=REPOSITORIES
    )
    assert result.target is not None
    assert result.target.kind == "pull_request"
    assert result.target.action == "synchronize"


def test_pull_request_only_action_cannot_be_classified_as_issue_work() -> None:
    raw = payload(action="synchronize")
    result = verify_delivery(secret=SECRET, headers=headers(raw), body=raw, repositories=REPOSITORIES)
    assert result.target is None
    assert result.observation.event_type == "issues"


@pytest.mark.parametrize("action", ["deleted", "assigned_unknown"])
def test_unhandled_action_is_authenticated_but_not_enqueued(action: str) -> None:
    raw = payload(action=action)
    result = verify_delivery(secret=SECRET, headers=headers(raw), body=raw, repositories=REPOSITORIES)
    assert result.target is None


def test_valid_ping_is_not_work() -> None:
    raw = b'{"repository":{"full_name":"acme/widget","id":42},"zen":"hello"}'
    result = verify_delivery(secret=SECRET, headers=headers(raw, event="ping"), body=raw, repositories=REPOSITORIES)
    assert result.target is None


@pytest.mark.parametrize("wrong", ["wrong-signature", "sha256=" + "a" * 64, "sha1=" + "a" * 40])
def test_invalid_signature_is_rejected(wrong: str) -> None:
    raw = payload()
    hs = headers(raw)
    hs[-1] = ("X-Hub-Signature-256", wrong)
    with pytest.raises(InvalidWebhook, match="invalid_signature"):
        verify_delivery(secret=SECRET, headers=hs, body=raw, repositories=REPOSITORIES)


def test_rejects_modified_body_after_signing() -> None:
    raw = payload()
    hs = headers(raw)
    modified = raw.replace(b"widget", b"widgex")
    with pytest.raises(InvalidWebhook):
        verify_delivery(secret=SECRET, headers=hs, body=modified, repositories=REPOSITORIES)


def test_rejects_duplicate_security_header_ignoring_case() -> None:
    raw = payload()
    hs = [*headers(raw), ("x-hub-signature-256", headers(raw)[-1][1])]
    with pytest.raises(InvalidWebhook, match="invalid_header"):
        verify_delivery(secret=SECRET, headers=hs, body=raw, repositories=REPOSITORIES)


@pytest.mark.parametrize("field", ["Content-Type", "Content-Length", "X-GitHub-Delivery", "X-GitHub-Event"])
def test_rejects_missing_required_headers(field: str) -> None:
    raw = payload()
    hs = [item for item in headers(raw) if item[0] != field]
    with pytest.raises(InvalidWebhook, match="invalid_header"):
        verify_delivery(secret=SECRET, headers=hs, body=raw, repositories=REPOSITORIES)


@pytest.mark.parametrize("value", ["application/x-www-form-urlencoded", "text/plain"])
def test_rejects_other_media_types(value: str) -> None:
    raw = payload()
    hs = [("Content-Type", value) if k == "Content-Type" else (k, v) for k, v in headers(raw)]
    with pytest.raises(InvalidWebhook, match="invalid_content_type"):
        verify_delivery(secret=SECRET, headers=hs, body=raw, repositories=REPOSITORIES)


def test_rejects_wrong_length_and_oversized_body() -> None:
    raw = payload()
    hs = [("Content-Length", "2") if k == "Content-Length" else (k, v) for k, v in headers(raw)]
    with pytest.raises(InvalidWebhook, match="invalid_length"):
        verify_delivery(secret=SECRET, headers=hs, body=raw, repositories=REPOSITORIES)
    large = b"x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(InvalidWebhook, match="body_limit"):
        verify_delivery(secret=SECRET, headers=headers(large), body=large, repositories=REPOSITORIES)


@pytest.mark.parametrize("repo_name,repo_id", [("other/widget", 42), ("acme/widget", 43)])
def test_rejects_repository_not_matching_both_scoped_name_and_id(repo_name: str, repo_id: int) -> None:
    raw = payload(repo_name=repo_name, repo_id=repo_id)
    with pytest.raises(InvalidWebhook, match="out_of_scope"):
        verify_delivery(secret=SECRET, headers=headers(raw), body=raw, repositories=REPOSITORIES)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"repository":{"full_name":"acme/widget","id":42},"action":"opened","issue":{}}',
        b'{"repository":{"full_name":"acme/widget","id":42},"action":"opened","number":7,"issue":{"number":8}}',
        b'{"repository":{"full_name":"acme/widget","id":42},"action":"opened","number":7,"issue":{"number":7,"updated_at":NaN}}',
        b'{"repository":{"full_name":"acme/widget","id":42},"action":"opened","number":7,"issue":{"number":7,"updated_at":"a"},"action":"closed"}',
        b"[]",
        b"\xff",
    ],
)
def test_refuses_malformed_or_ambiguous_json(raw: bytes) -> None:
    with pytest.raises(InvalidWebhook, match="invalid_payload"):
        verify_delivery(secret=SECRET, headers=headers(raw), body=raw, repositories=REPOSITORIES)


def test_rejects_unsupported_content_encoding() -> None:
    raw = payload()
    with pytest.raises(InvalidWebhook, match="unsupported_encoding"):
        verify_delivery(
            secret=SECRET, headers=[*headers(raw), ("Content-Encoding", "gzip")], body=raw, repositories=REPOSITORIES
        )


def test_secret_strength_and_empty_scope_fail_closed() -> None:
    raw = payload()
    with pytest.raises(ValueError):
        verify_delivery(secret=b"short", headers=headers(raw), body=raw, repositories=REPOSITORIES)
    with pytest.raises(ValueError):
        verify_delivery(secret=SECRET, headers=headers(raw), body=raw, repositories={})
