"""Safe remote publication contracts without host credentials or network writes."""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import _remote
from local_dev_mcp.tools.git.facades import remote_pr
from local_dev_mcp.tools.git.facades.remote_pr import (
    CreateFeaturePRTool,
    FetchRemoteMainTool,
    InspectRemoteTool,
    PushFeatureTool,
)

if TYPE_CHECKING:
    from local_dev_mcp.app.runtime import ServerSettings

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
A = "a" * 40
B = "b" * 40
C = "c" * 40


@dataclass
class MockRemote:
    root: Path = ROOT
    current_branch: str = "feat/example"
    head: str = B
    local_main: str = C
    tracking: str | None = A
    remote_main: str = A
    branch_head: str | None = None
    contains_base: bool = True
    includes_unpublished_main: bool = False
    records: list[tuple[str, ...]] = field(default_factory=lambda: cast(list[tuple[str, ...]], []))
    pr_list: list[dict[str, Any]] = field(default_factory=lambda: cast(list[dict[str, Any]], []))
    remote_changed: bool = False
    published_body: str | None = None
    commit_count: int = 1

    async def snapshot(self) -> tuple[str, str, str]:
        return self.current_branch, self.head, self.head

    async def repo(self) -> tuple[str, str]:
        return "example/project", "https://github.com/example/project"

    async def main_sha(self, _repository: str) -> str:
        return self.remote_main

    async def local_sha(self, ref: str) -> str | None:
        if ref == "refs/remotes/origin/main":
            return self.tracking
        if ref == "refs/heads/main":
            return self.local_main
        if ref == "HEAD":
            return self.head
        if ref == self.remote_main:
            return self.remote_main
        raise AssertionError(ref)

    async def branch(self, _branch: str) -> str | None:
        return self.branch_head

    async def ancestor(self, _base: str, _head: str) -> bool:
        if _base == C and _head == B:
            return self.includes_unpublished_main
        if _base == C and _head == A:
            return False
        return self.contains_base

    async def git(self, *args: str, write: bool = False, allow_failure: bool = False) -> _remote.Result:
        del write, allow_failure
        self.records.append(("git", *args))
        if args[0] == "symbolic-ref":
            return _remote.Result(1, "")
        if args[0] == "fetch":
            return _remote.Result(0, "")
        if args[0] == "update-ref":
            assert args[1:3] == ("--no-deref", "refs/remotes/origin/main")
            assert args[4] == self.tracking or (self.tracking is None and args[4] == "0" * 40)
            self.tracking = args[3]
            return _remote.Result(0, "")
        if args[0] == "push":
            self.branch_head = self.head
            return _remote.Result(0, "")
        if args[0] == "log":
            records = ["bcdefa"[i] * 40 + "\x00feat: step " + str(i) for i in range(self.commit_count)]
            return _remote.Result(0, "\x00".join(records) + "\x00")
        if args[0] == "diff":
            return _remote.Result(0, ".changes/example.toml\x00")
        if args[0] == "show":
            return _remote.Result(
                0,
                'impact = "minor"\nsummary = "Publish PR from local MCP"\n'
                'reason = "Automate GitHub publication."\nmigration = ""\n',
            )
        raise AssertionError(args)

    async def gh(self, *args: str, write: bool = False, input_text: str | None = None) -> _remote.Result:
        self.records.append(("gh", *args))
        if args[:2] == ("pr", "list"):
            return _remote.Result(0, json.dumps(self.pr_list))
        if args[:2] == ("pr", "create"):
            assert write
            assert "--draft" in args
            self.published_body = input_text
            self.pr_list = [
                {
                    "number": 7,
                    "url": "https://github.com/example/project/pull/7",
                    "headRefOid": self.head,
                    "baseRefName": "main",
                    "isDraft": True,
                    "state": "OPEN",
                }
            ]
            return _remote.Result(0, "https://github.com/example/project/pull/7\n")
        if args[:2] == ("pr", "edit"):
            assert write and input_text is not None
            self.published_body = input_text
            return _remote.Result(0, "")
        if args[:2] == ("pr", "view"):
            return _remote.Result(
                0, json.dumps({"body": self.published_body, "headRefOid": self.head, "baseRefOid": self.remote_main})
            )
        raise AssertionError(args)


def _tool(monkeypatch: pytest.MonkeyPatch, mock: MockRemote, kind: type[Any]) -> Any:
    def factory(_settings: object) -> MockRemote:
        return mock

    monkeypatch.setattr(remote_pr, "Remote", factory)
    policy = Policy(
        ROOT,
        (ROOT,),
        ("git", "gh", "git.remote.read", "git.remote.write", "gh.pr.create"),
    )
    return kind(cast("ServerSettings", SimpleNamespace(policy=policy)))


def test_branch_and_title_names_refuse_main_and_option_injection() -> None:
    for branch in (
        "main",
        "develop",
        "release/01.2.3",
        "release/1.2",
        "release/1.2.3/evil",
        "feat/../main",
        "--delete",
        "feat/a..b",
    ):
        with pytest.raises(ToolError):
            _remote.feature_name(branch)
    assert _remote.feature_name("feat/workflow-mvp") == "feat/workflow-mvp"
    assert _remote.feature_name("release/1.2.3") == "release/1.2.3"
    assert _remote.title("feat(workflow): enable guarded PRs")
    with pytest.raises(ToolError):
        _remote.title("feat: valid\n--force")


def test_inspection_reports_remote_divergence_without_modification(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote(contains_base=False, tracking=C)
    tool = _tool(monkeypatch, mock, InspectRemoteTool)
    data = asyncio.run(tool.execute())
    assert data["local_main"] == C and data["tracking_main"] == C
    assert data["tracking_is_fresh"] is False
    assert data["feature_contains_remote"] is False
    assert mock.records == []


def test_fetch_requires_observed_sha_and_only_updates_tracking(monkeypatch: pytest.MonkeyPatch) -> None:
    earlier = "d" * 40
    mock = MockRemote(tracking=earlier)
    tool = _tool(monkeypatch, mock, FetchRemoteMainTool)
    with pytest.raises(ToolError, match="changed"):
        asyncio.run(tool.execute("feat/example", B, C))
    assert mock.tracking == earlier
    result = asyncio.run(tool.execute("feat/example", B, A))
    assert result["tracking_main_after"] == A
    assert mock.local_main == C
    assert mock.records == [
        ("git", "symbolic-ref", "--quiet", "refs/remotes/origin/main"),
        (
            "git",
            "update-ref",
            "--no-deref",
            "refs/remotes/origin/main",
            A,
            earlier,
        ),
    ]


def test_fetch_rejects_rewritten_remote_main_without_touching_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote(tracking=C)
    tool = _tool(monkeypatch, mock, FetchRemoteMainTool)
    with pytest.raises(ToolError, match="rewritten or diverged"):
        asyncio.run(tool.execute("feat/example", B, A))
    assert mock.tracking == C
    assert not any(call[1] in ("fetch", "update-ref") for call in mock.records)


def test_push_requires_clean_named_branch_and_fresh_main(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote()
    tool = _tool(monkeypatch, mock, PushFeatureTool)
    with pytest.raises(ToolError):
        asyncio.run(tool.execute("main", B, A))
    with pytest.raises(ToolError, match="changed"):
        asyncio.run(tool.execute("feat/example", C, A))
    assert mock.records == []
    applied = asyncio.run(tool.execute("feat/example", B, A, None))
    assert applied["remote_after"] == B and applied["changed"] is True
    pushes = [row for row in mock.records if row[1] == "push"]
    assert len(pushes) == 1
    assert pushes[0] == (
        "git",
        "push",
        "--porcelain",
        "--no-follow-tags",
        "origin",
        B + ":refs/heads/feat/example",
    )
    existing = asyncio.run(tool.execute("feat/example", B, A, B))
    assert existing["changed"] is False


def test_push_rejects_remote_lease_mismatch_and_diverged_main(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote(branch_head=C)
    tool = _tool(monkeypatch, mock, PushFeatureTool)
    with pytest.raises(ToolError, match="changed"):
        asyncio.run(tool.execute("feat/example", B, A, None))
    assert not mock.records
    mock.branch_head = None
    mock.contains_base = False
    with pytest.raises(ToolError, match="does not contain"):
        asyncio.run(tool.execute("feat/example", B, A, None))
    assert not mock.records


def test_create_draft_uses_generated_body_and_is_retry_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote(branch_head=B)
    tool = _tool(monkeypatch, mock, CreateFeaturePRTool)
    result = asyncio.run(
        tool.execute(
            "feat/example",
            B,
            A,
            "feat(workflow): add publication tooling",
            purpose="Publish feature PRs with project-owned GitHub authentication.",
            exceptions=[
                {"type": "Activation", "detail": "Enable remote rules after first merge.", "evidence": "ADR 0006"}
            ],
        )
    )
    assert result["created"] is True and result["draft"] is True
    assert result["url"].endswith("/pull/7")
    assert mock.published_body is not None
    assert "## Release impact" in mock.published_body
    assert "## Activation" in mock.published_body
    assert "/commit/" + B in mock.published_body
    second = asyncio.run(tool.execute("feat/example", B, A, "feat(workflow): add publication tooling"))
    assert second["created"] is False and second["number"] == 7
    assert len([row for row in mock.records if row[:3] == ("gh", "pr", "create")]) == 1


def test_create_refuses_stale_refs_and_closed_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote(branch_head=C)
    tool = _tool(monkeypatch, mock, CreateFeaturePRTool)
    with pytest.raises(ToolError, match="not been pushed"):
        asyncio.run(tool.execute("feat/example", B, A, "feat(workflow): publish"))
    mock.branch_head = B
    mock.pr_list = [
        {
            "number": 5,
            "url": "https://github.com/example/project/pull/5",
            "headRefOid": B,
            "baseRefName": "main",
            "isDraft": False,
            "state": "CLOSED",
        }
    ]
    with pytest.raises(ToolError, match="already has"):
        asyncio.run(tool.execute("feat/example", B, A, "feat(workflow): publish"))
    assert not any(row[:3] == ("gh", "pr", "create") for row in mock.records)


def test_internal_runtime_must_enforce_scoped_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote()

    def factory(_settings: object) -> MockRemote:
        return mock

    monkeypatch.setattr(remote_pr, "Remote", factory)
    settings = cast("ServerSettings", SimpleNamespace(policy=Policy(ROOT, (ROOT,), ("git", "gh"))))
    with pytest.raises(ToolError):
        asyncio.run(InspectRemoteTool(settings).execute())
    with pytest.raises(ToolError):
        asyncio.run(FetchRemoteMainTool(settings).execute("feat/example", B, A))
    with pytest.raises(ToolError):
        asyncio.run(PushFeatureTool(settings).execute("feat/example", B, A))
    with pytest.raises(ToolError):
        asyncio.run(CreateFeaturePRTool(settings).execute("feat/example", B, A, "feat: test"))
    assert mock.records == []


def test_publication_refuses_unpublished_local_main_history(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote(includes_unpublished_main=True, branch_head=B)
    inspect = _tool(monkeypatch, mock, InspectRemoteTool)
    status = asyncio.run(inspect.execute())
    assert status["unpublished_main_in_feature"] is True

    push = _tool(monkeypatch, mock, PushFeatureTool)
    with pytest.raises(ToolError, match="local-main commits"):
        asyncio.run(push.execute("feat/example", B, A, B))

    create = _tool(monkeypatch, mock, CreateFeaturePRTool)
    with pytest.raises(ToolError, match="local-main commits"):
        asyncio.run(create.execute("feat/example", B, A, "feat(workflow): publish"))

    assert not any(row[:3] in (("gh", "pr", "create"), ("git", "push", "--porcelain")) for row in mock.records)


def test_long_history_gets_clickable_pr_commits_url(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MockRemote(branch_head=B, commit_count=6)
    tool = _tool(monkeypatch, mock, CreateFeaturePRTool)
    created = asyncio.run(tool.execute("feat/example", B, A, "feat(workflow): publish"))
    assert created["created"] and mock.published_body is not None
    assert "6 commits" in mock.published_body
    assert "https://github.com/example/project/pull/7/commits" in mock.published_body
    assert len([row for row in mock.records if row[:3] == ("gh", "pr", "edit")]) == 1


def test_create_checks_remote_base_after_creation_for_short_history(monkeypatch: pytest.MonkeyPatch) -> None:
    class MovingBaseRemote(MockRemote):
        async def gh(self, *args: str, write: bool = False, input_text: str | None = None) -> _remote.Result:
            result = await super().gh(*args, write=write, input_text=input_text)
            if args[:2] == ("pr", "view"):
                data = json.loads(result.output)
                data["baseRefOid"] = C
                return _remote.Result(0, json.dumps(data))
            return result

    mock = MovingBaseRemote(branch_head=B)
    tool = _tool(monkeypatch, mock, CreateFeaturePRTool)
    with pytest.raises(ToolError, match="confirmed"):
        asyncio.run(tool.execute("feat/example", B, A, "feat(git): publish branch"))
    assert len(mock.pr_list) == 1


@pytest.mark.parametrize(("field_name", "value"), [("state", "CLOSED"), ("baseRefName", "other")])
def test_create_rejects_changed_pr_identity_after_creation(
    monkeypatch: pytest.MonkeyPatch, field_name: str, value: str
) -> None:
    class ChangedPRRemote(MockRemote):
        async def gh(self, *args: str, write: bool = False, input_text: str | None = None) -> _remote.Result:
            result = await super().gh(*args, write=write, input_text=input_text)
            if args[:2] == ("pr", "create"):
                self.pr_list[0][field_name] = value
            return result

    mock = ChangedPRRemote(branch_head=B)
    tool = _tool(monkeypatch, mock, CreateFeaturePRTool)
    with pytest.raises(ToolError, match="confirmed"):
        asyncio.run(tool.execute("feat/example", B, A, "feat(git): publish branch"))


def test_pr_facts_accept_release_assessment_default_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    class DefaultMigrationRemote(MockRemote):
        async def git(self, *args: str, write: bool = False, allow_failure: bool = False) -> _remote.Result:
            if args[0] == "show":
                return _remote.Result(
                    0,
                    'impact = "minor"\nsummary = "New tool"\nreason = "Preserve existing contracts."\n'
                    'contract = "MCP"\nassessed_by = "agent"\n',
                )
            return await super().git(*args, write=write, allow_failure=allow_failure)

    mock = DefaultMigrationRemote(branch_head=B)
    tool = _tool(monkeypatch, mock, CreateFeaturePRTool)
    result = asyncio.run(tool.execute("feat/example", B, A, "feat: publish default migration"))
    assert result["created"] is True and result["draft"] is True
    assert mock.published_body is not None


def test_release_pr_requires_verified_receipt_and_renders_distinct_body(monkeypatch: pytest.MonkeyPatch) -> None:
    verified: list[tuple[str, str]] = []

    async def check(_read_git: Any, base: str, head: str) -> dict[str, Any]:
        verified.append((base, head))
        return {
            "kind": "release",
            "plan": {
                "current_version": "0.1.0",
                "next_version": "0.2.0",
                "maximum_impact": "major",
                "digest": "d" * 64,
            },
            "assessments": ["first", "second"],
        }

    monkeypatch.setattr(remote_pr, "check_integration", check)
    mock = MockRemote(current_branch="release/0.2.0")
    pushed = asyncio.run(_tool(monkeypatch, mock, PushFeatureTool).execute("release/0.2.0", B, A))
    assert pushed["changed"] is True and mock.branch_head == B
    created = asyncio.run(
        _tool(monkeypatch, mock, CreateFeaturePRTool).execute("release/0.2.0", B, A, "chore(release): prepare v0.2.0")
    )
    assert created["created"] is True and mock.published_body is not None
    assert "verified 0.2.0 release" in mock.published_body
    assert "Consume 2 assessed changes" in mock.published_body
    assert "Maximum assessed impact: `major`" in mock.published_body
    assert verified == [(A, B), (A, B)]


def test_release_branch_rejects_nonrelease_snapshot_before_push(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(_read_git: Any, _base: str, _head: str) -> dict[str, Any]:
        return {"kind": "change", "plan": None, "assessments": []}

    monkeypatch.setattr(remote_pr, "check_integration", check)
    mock = MockRemote(current_branch="release/0.2.0")
    with pytest.raises(ToolError, match="verified release"):
        asyncio.run(_tool(monkeypatch, mock, PushFeatureTool).execute("release/0.2.0", B, A))
    assert not any(call[1] == "push" for call in mock.records)


def test_release_branch_rejects_mismatched_receipt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(_read_git: Any, _base: str, _head: str) -> dict[str, Any]:
        return {
            "kind": "release",
            "plan": {
                "current_version": "0.1.0",
                "next_version": "0.3.0",
                "maximum_impact": "minor",
                "digest": "d" * 64,
            },
            "assessments": ["first"],
        }

    monkeypatch.setattr(remote_pr, "check_integration", check)
    mock = MockRemote(current_branch="release/0.2.0")
    with pytest.raises(ToolError, match="branch version"):
        asyncio.run(_tool(monkeypatch, mock, PushFeatureTool).execute("release/0.2.0", B, A))
    assert not any(call[1] == "push" for call in mock.records)
