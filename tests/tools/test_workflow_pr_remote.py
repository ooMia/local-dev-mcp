"""Remote-main invariants for PR metadata and synchronization."""

import json
from pathlib import Path
from typing import Any

import pytest

from local_dev_mcp.tools import workflow_pr

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
A = "a" * 40
B = "b" * 40
C = "c" * 40


def _commands(remote_sha: str, ancestor: str = A) -> tuple[list[tuple[str, ...]], Any]:
    calls: list[tuple[str, ...]] = []

    def run(_root: Path, *args: str, capture: bool = False, input_text: str | None = None) -> str:
        del capture, input_text
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "--verify"):
            return A if args[-1].startswith("origin/main") else B
        if args[:2] == ("git", "merge-base"):
            return ancestor
        if args[:3] == ("gh", "repo", "view"):
            return '{"url":"https://github.com/example/project"}'
        if args[:2] == ("gh", "api"):
            assert args[-1] == "repos/example/project/git/ref/heads/main"
            return json.dumps({"object": {"sha": remote_sha}})
        if args[:2] == ("git", "log"):
            return B + "\x00feat: example\x00"
        if args[:2] == ("git", "diff"):
            return ".changes/example.toml\x00"
        if args[:2] == ("git", "show"):
            return 'impact = "minor"\nsummary = "Example change"\nreason = "Example reason"\nmigration = ""\n'
        raise AssertionError(args)

    return calls, run


def test_rejects_stale_remote_tracking_base() -> None:
    calls, run = _commands(C)
    with pytest.raises(ValueError, match="Base differs from remote main"):
        workflow_pr.collect(ROOT, run, "origin/main", "HEAD")
    assert any(c[:2] == ("gh", "api") for c in calls)
    assert not any(c[:2] == ("git", "diff") for c in calls)


def test_rejects_divergent_feature_even_with_fresh_remote_tracking_ref() -> None:
    calls, run = _commands(A, C)
    with pytest.raises(ValueError, match="does not contain the base"):
        workflow_pr.collect(ROOT, run, "origin/main", "HEAD")
    assert not any(c[:2] == ("gh", "api") for c in calls)


def test_matching_remote_base_collects_exact_assessment_and_history() -> None:
    calls, run = _commands(A)
    data = workflow_pr.collect(ROOT, run, "origin/main", "HEAD")
    assert data["base"] == A
    assert data["head"] == B
    assert [item["id"] for item in data["assessments"]] == ["example"]
    assert len(data["commits"]) == 1
    assert any(c[:2] == ("gh", "api") for c in calls)


def test_sync_distinguishes_remote_branch_name_from_local_tracking_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot: dict[str, Any] = {
        "schema": 1,
        "repo_url": "https://github.com/example/project",
        "base": A,
        "head": B,
        "merge_base": A,
        "commits": [{"sha": B, "title": "feat: example"}],
        "assessments": [{"id": "example", "impact": "minor", "summary": "Example", "reason": "Why", "migration": ""}],
    }
    body = workflow_pr.render(ROOT, snapshot)
    metadata = {
        "body": body,
        "headRefOid": B,
        "baseRefOid": A,
        "baseRefName": "main",
        "state": "OPEN",
        "url": "https://github.com/example/project/pull/9",
    }
    observed: list[tuple[str, str]] = []

    def collect(_root: Path, _run: workflow_pr.Run, base: str, head: str) -> dict[str, Any]:
        observed.append((base, head))
        return snapshot

    def run(_root: Path, *args: str, capture: bool = False, input_text: str | None = None) -> str:
        del capture, input_text
        if args[:3] == ("gh", "pr", "view"):
            return json.dumps(metadata)
        raise AssertionError(args)

    monkeypatch.setattr(workflow_pr, "collect", collect)
    assert workflow_pr.sync(ROOT, run, 9, "main") == body
    assert observed == [("origin/main", "HEAD")]
