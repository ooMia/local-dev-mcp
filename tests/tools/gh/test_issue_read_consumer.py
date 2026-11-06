"""The first GitHub Consumer is read-only and bound to an explicit Receipt target."""

import asyncio
import http.client
import json
from dataclasses import replace

import pytest

from local_dev_mcp.tools.gh.delivery.issue_read_consumer import (
    API_VERSION,
    MAX_RESPONSE_BYTES,
    GitHubIssueHttpTransport,
    IssueReadHold,
    bind_issue_read_contract,
    decode_issue_read_checkpoint,
    issue_read_input_digest,
    make_issue_read_intent,
)
from local_dev_mcp.tools.gh.delivery.operation_checkpoint import OperationCheckpoint
from local_dev_mcp.tools.gh.delivery.operation_contracts import ContractCatalog
from local_dev_mcp.tools.gh.delivery.operation_plan import OperationNode, PlanStructure
from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget

pytestmark = pytest.mark.unit
EVIDENCE = "sha256:" + "a" * 64


def target() -> VerifiedTarget:
    return VerifiedTarget("issue", "ooMia/local-dev-mcp", 1234, 42, "opened", "2026-11-03T00:00:00Z")


def node() -> OperationNode:
    return OperationNode("read", "github_issue", "read_current", "read:v1")


def response(*, state: str = "open") -> bytes:
    return json.dumps(
        {
            "id": 56789,
            "number": 42,
            "repository_url": "https://api.github.com/repos/ooMia/local-dev-mcp",
            "state": state,
            "updated_at": "2026-11-03T01:00:00Z",
            "labels": [{"name": "bug"}, {"name": "triaged"}],
        },
        sort_keys=True,
    ).encode()


def test_read_intent_binds_all_envelope_fields_and_is_payload_free() -> None:
    intent = make_issue_read_intent(operation=node(), target=target())
    assert intent.input_digest == issue_read_input_digest(target())
    assert intent.operation == node()
    assert not intent.consumed_outputs
    assert b"ooMia" not in intent.canonical_bytes()
    assert b'"number":42' not in intent.canonical_bytes()
    assert b"opened" not in intent.canonical_bytes()
    for updated in (
        replace(target(), repository_id=999),
        replace(target(), number=43),
        replace(target(), action="edited"),
        replace(target(), updated_at="2026-11-04T00:00:00Z"),
    ):
        assert issue_read_input_digest(updated) != intent.input_digest
    with pytest.raises(IssueReadHold, match="unsupported"):
        make_issue_read_intent(
            operation=OperationNode("read", "github_issue", "read_current", "read:v2"), target=target()
        )
    with pytest.raises(IssueReadHold, match="unsupported"):
        make_issue_read_intent(operation=replace(node(), depends_on=("other",)), target=target())
    with pytest.raises(IssueReadHold, match="target"):
        make_issue_read_intent(operation=node(), target=replace(target(), kind="pull_request"))
    with pytest.raises(IssueReadHold, match="target"):
        make_issue_read_intent(operation=node(), target=replace(target(), repository="evil/path/extra"))


def test_read_checkpoint_contains_digests_not_observed_data() -> None:
    intent = make_issue_read_intent(operation=node(), target=target())
    checkpoint = decode_issue_read_checkpoint(raw=response(), target=target(), intent=intent)
    assert checkpoint.operation_id == "read"
    assert checkpoint.intent_digest == intent.intent_digest
    assert [item.field_id for item in checkpoint.fields] == ["issue_id", "number", "state", "updated_at", "labels"]
    assert checkpoint == OperationCheckpoint.from_canonical_bytes(checkpoint.canonical_bytes())
    for value in (b"bug", b"triaged", b"ooMia", b"2026-11-03"):
        assert value not in checkpoint.canonical_bytes()
    changed = decode_issue_read_checkpoint(raw=response(state="closed"), target=target(), intent=intent)
    assert changed != checkpoint


@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"number": True},
        {"number": 43},
        {"repository_url": "https://api.github.com/repos/other/repo"},
        {"state": "invalid"},
        {"updated_at": "2026-02-30T00:00:00Z"},
        {"labels": ["bug"]},
        {"labels": [{"name": "Bug"}, {"name": "bug"}]},
        {"labels": [{"name": "bad" + chr(10) + "label"}]},
        {"pull_request": {}},
    ],
)
def test_rejects_incorrect_identity_shape_and_non_issue_objects(changes: dict[str, object]) -> None:
    intent = make_issue_read_intent(operation=node(), target=target())
    body = json.loads(response())
    body.update(changes)
    with pytest.raises(IssueReadHold):
        decode_issue_read_checkpoint(raw=json.dumps(body).encode(), target=target(), intent=intent)


def test_rejects_noncanonical_or_oversized_response_and_wrong_intent() -> None:
    intent = make_issue_read_intent(operation=node(), target=target())
    for raw in (b"{}", b'{"id":1,"id":2}', b"", b"\xff", b"a" * (MAX_RESPONSE_BYTES + 1)):
        with pytest.raises(IssueReadHold):
            decode_issue_read_checkpoint(raw=raw, target=target(), intent=intent)
    other_intent = replace(intent, input_digest=EVIDENCE)
    with pytest.raises(IssueReadHold, match="intent_mismatch"):
        decode_issue_read_checkpoint(raw=response(), target=target(), intent=other_intent)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b'"name": "bug"', b'"name": "bug", "name": "triaged"'),
        (b'"labels":', b'"ignored": {"x": 1, "x": 2}, "labels":'),
        (b'"state": "open"', b'"state": "open", "state": "closed"'),
        (b'"name": "bug"', b'"name": NaN'),
        (b'"name": "bug"', b'"name": Infinity'),
        (b'"name": "bug"', b'"name": -Infinity'),
        (b'"labels":', b'"ignored": {"x": NaN}, "labels":'),
        (b'"name": "bug"', b'"name": "\xff"'),
    ],
)
def test_read_decoder_keeps_strict_json_validation_for_nested_and_ignored_fields(before: bytes, after: bytes) -> None:
    """The extracted JSON hooks must reject ambiguity before shape filtering."""
    raw = response()
    assert raw.count(before) == 1
    with pytest.raises(IssueReadHold, match="^invalid_issue_response$"):
        decode_issue_read_checkpoint(
            raw=raw.replace(before, after, 1),
            target=target(),
            intent=make_issue_read_intent(operation=node(), target=target()),
        )


def test_contract_hooks_are_explicit_per_target_and_observation_drifts_hold() -> None:
    async def run() -> None:
        intent = make_issue_read_intent(operation=node(), target=target())
        calls: list[VerifiedTarget] = []
        bodies = [response(), response(), response(state="closed")]

        async def fake_transport(value: VerifiedTarget) -> bytes:
            calls.append(value)
            return bodies.pop(0)

        contract = bind_issue_read_contract(
            intent=intent, target=target(), transport=fake_transport, test_evidence_digest=EVIDENCE
        )
        catalog = ContractCatalog((contract,))
        assert catalog.inspect_plan(PlanStructure((node(),)), intents=(intent,)).compatible
        assert not calls  # Static compatibility must never invoke any hook.
        assert contract.concurrency.guard is not None
        assert contract.execute is not None
        assert contract.decode is not None
        assert contract.verify is not None
        assert contract.probe is not None
        await contract.concurrency.guard(intent)
        assert not calls
        raw = await contract.execute(intent)
        assert type(raw) is bytes
        checkpoint = contract.decode(raw)
        assert await contract.verify(intent, checkpoint)
        assert not await contract.verify(intent, checkpoint)
        assert len(calls) == 3
        with pytest.raises(IssueReadHold, match="intent_mismatch"):
            await contract.execute(replace(intent, input_digest=EVIDENCE))
        with pytest.raises(IssueReadHold, match="intent_mismatch"):
            await contract.probe(replace(intent, input_digest=EVIDENCE))
        assert len(calls) == 3

    asyncio.run(run())


def test_contract_rejects_invalid_binding_before_network_access() -> None:
    intent = make_issue_read_intent(operation=node(), target=target())

    async def forbidden(_: VerifiedTarget) -> bytes:
        raise AssertionError("Must not call transport")

    with pytest.raises(IssueReadHold, match="intent_mismatch"):
        bind_issue_read_contract(
            intent=intent,
            target=replace(target(), number=99),
            transport=forbidden,
            test_evidence_digest=EVIDENCE,
        )
    with pytest.raises(ValueError, match="digest"):
        bind_issue_read_contract(intent=intent, target=target(), transport=forbidden, test_evidence_digest="unverified")


def test_transport_is_explicit_get_only_and_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, str, str, dict[str, str], int]] = []

    class FakeResponse:
        status = 200

        def read(self, size: int) -> bytes:
            assert size == MAX_RESPONSE_BYTES + 1
            return response()

    class FakeConnection:
        def __init__(self, host: str, *, timeout: int) -> None:
            self.host = host
            self.timeout = timeout

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            requests.append((self.host, method, path, headers, self.timeout))

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)

    async def run() -> None:
        transport = GitHubIssueHttpTransport(token="test-secret")
        assert "test-secret" not in repr(transport)
        assert await transport(target()) == response()
        assert len(requests) == 1
        host, method, path, headers, timeout = requests[0]
        assert (host, method, path, timeout) == (
            "api.github.com",
            "GET",
            "/repos/ooMia/local-dev-mcp/issues/42",
            10,
        )
        assert headers["Authorization"] == "Bearer test-secret"
        assert headers["X-GitHub-Api-Version"] == API_VERSION
        assert headers["Accept"] == "application/vnd.github+json"
        with pytest.raises(ValueError):
            GitHubIssueHttpTransport(token="bad\r\nheader")

    asyncio.run(run())


def test_remote_http_errors_are_hold_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 302

    class FakeConnection:
        def __init__(self, host: str, *, timeout: int) -> None:
            del host, timeout

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            del method, path, headers

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)
    with pytest.raises(IssueReadHold, match="http_status"):
        asyncio.run(GitHubIssueHttpTransport()(target()))
