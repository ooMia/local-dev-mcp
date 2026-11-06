"""Q77 goal-state semantics with fake remote writes and explicit concurrency scope."""

import asyncio
import http.client
import json
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import pytest

from local_dev_mcp.tools.gh.delivery.issue_label_consumer import (
    AttemptPermit,
    GitHubIssueLabelHttpTransport,
    IssueLabelHold,
    IssueLabelProbe,
    bind_issue_add_label_contract,
    make_issue_add_label_intent,
)
from local_dev_mcp.tools.gh.delivery.issue_label_scope import issue_label_lock_key
from local_dev_mcp.tools.gh.delivery.issue_read_consumer import (
    decode_issue_read_checkpoint,
    make_issue_read_intent,
)
from local_dev_mcp.tools.gh.delivery.operation_checkpoint import OperationCheckpoint
from local_dev_mcp.tools.gh.delivery.operation_contracts import ContractCatalog
from local_dev_mcp.tools.gh.delivery.operation_intent import OperationIntent
from local_dev_mcp.tools.gh.delivery.operation_lineage import OperationLineage
from local_dev_mcp.tools.gh.delivery.operation_plan import OperationNode, PlanStructure
from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget

pytestmark = pytest.mark.unit
EVIDENCE = "sha256:" + "a" * 64


def target() -> VerifiedTarget:
    return VerifiedTarget("issue", "ooMia/local-dev-mcp", 1234, 42, "opened", "2026-11-03T00:00:00Z")


def operations() -> tuple[OperationNode, OperationNode]:
    return (
        OperationNode("read", "github_issue", "read_current", "read:v1"),
        OperationNode("label", "github_issue", "add_label", "add_label:v2", ("read",)),
    )


def response(labels: tuple[str, ...]) -> bytes:
    return json.dumps(
        {
            "id": 56789,
            "number": 42,
            "repository_url": "https://api.github.com/repos/ooMia/local-dev-mcp",
            "state": "open",
            "updated_at": "2026-11-03T01:00:00Z",
            "labels": [{"name": name} for name in labels],
        },
        sort_keys=True,
    ).encode()


def evidence() -> tuple[OperationIntent, OperationCheckpoint, OperationIntent, OperationLineage]:
    read, add = operations()
    source_intent = make_issue_read_intent(operation=read, target=target())
    source_checkpoint = decode_issue_read_checkpoint(raw=response(("bug",)), target=target(), intent=source_intent)
    intent, lineage = make_issue_add_label_intent(
        operation=add,
        target=target(),
        label="triaged",
        source_intent=source_intent,
        source_checkpoint=source_checkpoint,
    )
    return source_intent, source_checkpoint, intent, lineage


class FakeRemote:
    def __init__(self, labels: tuple[str, ...] = ("bug",)) -> None:
        self.labels: set[str] = set(labels)
        self.gets = 0
        self.posts = 0
        self.attempts = 0
        self.scope_entries = 0
        self.active = False
        self.fail_after_post = False
        self.remove_after_post = False
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def scoped(self, intent: OperationIntent) -> AsyncGenerator[AttemptPermit]:
        assert intent.operation.operation_type == "add_label"
        async with self.lock:
            self.scope_entries += 1
            self.active = True
            try:
                yield self
            finally:
                self.active = False

    async def observe(self, value: VerifiedTarget) -> bytes:
        assert value == target()
        self.gets += 1
        return response(tuple(sorted(self.labels)))

    async def commit_attempt(self) -> None:
        assert self.active
        if self.attempts:
            raise IssueLabelHold("fake_attempt_already_recorded")
        self.attempts += 1

    async def add(self, value: VerifiedTarget, label: str) -> object:
        assert self.active and self.attempts == 1, "POST requires a committed pre-effect Attempt"
        assert value == target() and label == "triaged"
        self.posts += 1
        self.labels.add(label)
        if self.remove_after_post:
            self.labels.discard(label)
        if self.fail_after_post:
            raise ConnectionError("simulated response loss")
        return None


def bind(remote: FakeRemote) -> tuple[OperationIntent, OperationLineage, Any]:
    source_intent, source_checkpoint, intent, lineage = evidence()
    return (
        intent,
        lineage,
        bind_issue_add_label_contract(
            intent=intent,
            target=target(),
            label="triaged",
            source_intent=source_intent,
            source_checkpoint=source_checkpoint,
            observe=remote.observe,
            add_label=remote.add,
            write_scope=remote.scoped,
            test_evidence_digest=EVIDENCE,
        ),
    )


def test_intent_and_lineage_bind_source_read_checkpoint_and_goal() -> None:
    source_intent, source_checkpoint, intent, lineage = evidence()
    _, add = operations()
    assert intent.operation == add
    assert intent.consumed_outputs[0].checkpoint_digest == source_checkpoint.checkpoint_digest
    assert lineage.bindings[0].source_checkpoint_digest == source_checkpoint.checkpoint_digest
    assert lineage.bindings[0].source_field_id == "labels"
    assert lineage.bindings[0].source_field_digest == next(
        f.value_digest for f in source_checkpoint.fields if f.field_id == "labels"
    )
    assert OperationIntent.from_canonical_bytes(intent.canonical_bytes()) == intent
    assert OperationLineage.from_canonical_bytes(lineage.canonical_bytes()) == lineage
    assert b"triaged" not in intent.canonical_bytes()
    assert b"ooMia" not in intent.canonical_bytes()

    with pytest.raises(IssueLabelHold):
        make_issue_add_label_intent(
            operation=replace(add, depends_on=()),
            target=target(),
            label="triaged",
            source_intent=source_intent,
            source_checkpoint=source_checkpoint,
        )
    with pytest.raises(IssueLabelHold):
        make_issue_add_label_intent(
            operation=add,
            target=replace(target(), number=43),
            label="triaged",
            source_intent=source_intent,
            source_checkpoint=source_checkpoint,
        )
    with pytest.raises(ValueError):
        make_issue_add_label_intent(
            operation=add,
            target=target(),
            label="bad\nlabel",
            source_intent=source_intent,
            source_checkpoint=source_checkpoint,
        )
    with pytest.raises(IssueLabelHold):
        make_issue_add_label_intent(
            operation=add,
            target=target(),
            label="triaged",
            source_intent=source_intent,
            source_checkpoint=replace(source_checkpoint, output_schema_version="wrong:v1"),
        )


def test_concurrency_catalog_is_static_and_goal_satisfied_without_post() -> None:
    async def run() -> None:
        remote = FakeRemote(("bug", "triaged"))
        intent, _, contract = bind(remote)
        catalog = ContractCatalog((contract,))
        assert catalog.inspect_plan(PlanStructure(operations()), intents=(intent,)).status == "hold"
        assert catalog.inspect(operations()[1], intent=intent).compatible
        assert (remote.gets, remote.posts, remote.scope_entries) == (0, 0, 0)
        assert contract.concurrency.guard is not None
        assert contract.execute is not None
        assert contract.decode is not None
        assert contract.verify is not None
        assert contract.probe is not None
        await contract.concurrency.guard(intent)
        raw = await contract.execute(intent)
        assert type(raw) is bytes
        checkpoint = contract.decode(raw)
        assert await contract.verify(intent, checkpoint)
        assert checkpoint.operation_id == intent.operation.operation_id
        assert checkpoint.intent_digest == intent.intent_digest
        assert b"triaged" not in checkpoint.canonical_bytes()
        assert (remote.posts, remote.gets, remote.scope_entries) == (0, 2, 2)
        with pytest.raises(IssueLabelHold, match="already_attempted"):
            await contract.execute(intent)

    asyncio.run(run())


def test_missing_goal_posts_once_under_scope_then_rechecks() -> None:
    async def run() -> None:
        remote = FakeRemote()
        intent, _, contract = bind(remote)
        assert contract.execute is not None
        assert contract.decode is not None
        assert contract.verify is not None
        raw = await contract.execute(intent)
        assert type(raw) is bytes
        assert remote.posts == 1
        assert remote.gets == 2
        assert "triaged" in remote.labels
        checkpoint = contract.decode(raw)
        assert await contract.verify(intent, checkpoint)
        assert remote.gets == 3
        assert checkpoint == OperationCheckpoint.from_canonical_bytes(checkpoint.canonical_bytes())
        with pytest.raises(IssueLabelHold, match="already_attempted"):
            await contract.execute(intent)
        assert remote.posts == 1

    asyncio.run(run())


def test_response_lost_probe_accepts_goal_without_attributing_mutation_or_reposting() -> None:
    async def run() -> None:
        remote = FakeRemote()
        remote.fail_after_post = True
        intent, _, contract = bind(remote)
        assert contract.execute is not None and contract.probe is not None
        with pytest.raises(IssueLabelHold, match="remote_outcome_unknown"):
            await contract.execute(intent)
        assert remote.posts == 1
        result = await contract.probe(intent)
        assert isinstance(result, IssueLabelProbe)
        assert result.status == "goal_satisfied"
        assert result.mutation_attributed is False
        assert remote.posts == 1
        with pytest.raises(IssueLabelHold, match="already_attempted"):
            await contract.execute(intent)
        assert remote.posts == 1

    asyncio.run(run())


def test_probe_absence_is_inconclusive_and_never_permits_automatic_retry() -> None:
    async def run() -> None:
        remote = FakeRemote()
        intent, _, contract = bind(remote)
        assert contract.probe is not None
        result = await contract.probe(intent)
        assert isinstance(result, IssueLabelProbe)
        assert result.status == "inconclusive"
        assert result.mutation_attributed is False
        assert remote.posts == 0
        remote.remove_after_post = True
        assert contract.execute is not None
        with pytest.raises(IssueLabelHold, match="postcondition_unverified"):
            await contract.execute(intent)
        result2 = await contract.probe(intent)
        assert isinstance(result2, IssueLabelProbe) and result2.status == "inconclusive"
        assert remote.posts == 1
        with pytest.raises(IssueLabelHold, match="already_attempted"):
            await contract.execute(intent)

    asyncio.run(run())


def test_bad_observation_and_wrong_binding_fail_before_write() -> None:
    async def run() -> None:
        remote = FakeRemote()
        source_intent, source_checkpoint, intent, _ = evidence()
        with pytest.raises(IssueLabelHold, match="intent_mismatch"):
            bind_issue_add_label_contract(
                intent=replace(intent, effect_digest=EVIDENCE),
                target=target(),
                label="triaged",
                source_intent=source_intent,
                source_checkpoint=source_checkpoint,
                observe=remote.observe,
                add_label=remote.add,
                write_scope=remote.scoped,
                test_evidence_digest=EVIDENCE,
            )

        async def bad_issue(_: VerifiedTarget) -> bytes:
            return response(("bug",)).replace(b'"id": 56789', b'"id": 999')

        bound = bind_issue_add_label_contract(
            intent=intent,
            target=target(),
            label="triaged",
            source_intent=source_intent,
            source_checkpoint=source_checkpoint,
            observe=bad_issue,
            add_label=remote.add,
            write_scope=remote.scoped,
            test_evidence_digest=EVIDENCE,
        )
        assert bound.execute is not None
        with pytest.raises(IssueLabelHold, match="identity_changed"):
            await bound.execute(intent)
        assert remote.posts == 0

        _, _, contract = bind(remote)
        assert contract.probe is not None
        with pytest.raises(IssueLabelHold, match="intent_mismatch"):
            await contract.probe(replace(intent, input_digest=EVIDENCE))

    asyncio.run(run())


def test_missing_concurrency_scope_cannot_bind_writer() -> None:
    remote = FakeRemote()
    source_intent, source_checkpoint, intent, _ = evidence()
    with pytest.raises(TypeError, match="Explicit"):
        bind_issue_add_label_contract(
            intent=intent,
            target=target(),
            label="triaged",
            source_intent=source_intent,
            source_checkpoint=source_checkpoint,
            observe=remote.observe,
            add_label=remote.add,
            write_scope=cast(Any, None),
            test_evidence_digest=EVIDENCE,
        )


def test_concurrent_fake_consumers_share_scope_and_add_label_once() -> None:
    async def run() -> None:
        remote = FakeRemote()
        first_intent, _, first = bind(remote)
        second_intent, _, second = bind(remote)
        assert first.execute is not None and second.execute is not None
        assert first.decode is not None and second.decode is not None

        first_result, second_result = cast(
            tuple[object, object], await asyncio.gather(first.execute(first_intent), second.execute(second_intent))
        )
        assert type(first_result) is bytes and type(second_result) is bytes
        assert first.decode(first_result).intent_digest == first_intent.intent_digest
        assert second.decode(second_result).intent_digest == second_intent.intent_digest
        assert remote.scope_entries == 2
        assert remote.posts == 1
        assert remote.labels == {"bug", "triaged"}

    asyncio.run(run())


def test_goal_verify_tolerates_unrelated_label_changes_but_not_goal_removal() -> None:
    async def run() -> None:
        remote = FakeRemote()
        intent, _, contract = bind(remote)
        assert contract.execute is not None and contract.decode is not None
        assert contract.verify is not None
        raw = await contract.execute(intent)
        assert type(raw) is bytes
        checkpoint = contract.decode(raw)

        remote.labels.add("unrelated")
        assert await contract.verify(intent, checkpoint)
        remote.labels.discard("triaged")
        assert not await contract.verify(intent, checkpoint)
        assert remote.posts == 1

    asyncio.run(run())


def test_issue_label_lock_key_is_stable_for_repository_identity() -> None:
    original = target()
    key = issue_label_lock_key(original)
    assert type(key) is int and -(2**63) <= key < 2**63
    assert issue_label_lock_key(replace(original, action="edited")) == key
    assert issue_label_lock_key(replace(original, repository="renamed/repository")) == key
    assert issue_label_lock_key(replace(original, number=43)) != key
    assert issue_label_lock_key(replace(original, repository_id=999)) != key


def test_http_add_label_is_explicit_single_post_and_rejects_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, int, str, str, bytes, dict[str, str]]] = []
    status = 200

    class FakeResponse:
        @property
        def status(self) -> int:
            return status

    class FakeConnection:
        def __init__(self, host: str, *, timeout: int) -> None:
            self.host = host
            self.timeout = timeout

        def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            requests.append((self.host, self.timeout, method, path, body, headers))

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)

    async def run() -> None:
        nonlocal status
        sender = GitHubIssueLabelHttpTransport(token="secret-test-token")
        assert "secret-test-token" not in repr(sender)
        assert await sender(target(), "triaged") is None
        assert len(requests) == 1
        host, timeout, method, path, body, headers = requests[0]
        assert (host, timeout, method, path) == (
            "api.github.com",
            10,
            "POST",
            "/repos/ooMia/local-dev-mcp/issues/42/labels",
        )
        assert json.loads(body) == {"labels": ["triaged"]}
        assert headers["Authorization"] == "Bearer secret-test-token"
        assert headers["Content-Type"] == "application/json"
        assert headers["X-GitHub-Api-Version"] == "2026-03-10"
        status = 301
        with pytest.raises(IssueLabelHold, match="http_status_unknown"):
            await sender(target(), "triaged")
        assert len(requests) == 2  # No redirect following or automatic retry.
        with pytest.raises(ValueError):
            GitHubIssueLabelHttpTransport(token="bad" + chr(10) + "token")
        with pytest.raises(ValueError):
            GitHubIssueLabelHttpTransport(token="")
        with pytest.raises(ValueError):
            await sender(target(), "bad" + chr(13) + "label")
        assert len(requests) == 2

    asyncio.run(run())


def test_http_post_cancellation_drains_in_flight_request_before_scope_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    in_scope = False

    class FakeResponse:
        status = 200

    class FakeConnection:
        def __init__(self, host: str, *, timeout: int) -> None:
            assert host == "api.github.com" and timeout == 10

        def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            del path, body, headers
            assert method == "POST"
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test request timed out")

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)

    async def run() -> None:
        nonlocal in_scope
        sender = GitHubIssueLabelHttpTransport(token="test-secret")

        async def operation() -> None:
            nonlocal in_scope
            in_scope = True
            try:
                await sender(target(), "triaged")
            finally:
                in_scope = False

        task = asyncio.create_task(operation())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert in_scope
            assert not task.done(), "Cancelled await must hold the scope until the POST thread finishes"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not in_scope

    asyncio.run(run())


def test_failed_attempt_commit_never_starts_remote_post() -> None:
    class FailingAttemptRemote(FakeRemote):
        async def commit_attempt(self) -> None:
            assert self.active
            raise IssueLabelHold("simulated_attempt_commit_failure")

    async def run() -> None:
        remote = FailingAttemptRemote()
        intent, _, contract = bind(remote)
        assert contract.execute is not None
        with pytest.raises(IssueLabelHold, match="simulated_attempt_commit_failure"):
            await contract.execute(intent)
        assert remote.gets == 1
        assert remote.attempts == 0
        assert remote.posts == 0
        with pytest.raises(IssueLabelHold, match="already_attempted"):
            await contract.execute(intent)
        assert remote.posts == 0

    asyncio.run(run())
