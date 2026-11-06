"""The GitHub boundary emits conditional merges and composes native rules."""

import json
from pathlib import Path

import pytest

from local_dev_mcp.tools import workflow

pytestmark = pytest.mark.unit


def test_pr_merge_pins_observed_head_and_verifies_real_result(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    payloads: list[dict[str, object]] = []
    message = "feat: example (#12)\n\nSource-PR: https://github.com/owner/project/pull/12\nSource-Head: " + "a" * 40

    def remote(root: Path, *argv: str, capture: bool = False, input_text: str | None = None) -> str:
        calls.append(argv)
        if argv[:3] == ("git", "remote", "get-url"):
            return "git@github.com:owner/project.git"
        if argv[:3] == ("gh", "repo", "view"):
            return '{"nameWithOwner":"owner/project","url":"https://github.com/owner/project"}'
        if argv[:3] == ("gh", "pr", "view"):
            return json.dumps(
                {
                    "url": "https://github.com/owner/project/pull/12",
                    "title": "feat: example",
                    "headRefOid": "a" * 40,
                    "baseRefName": "main",
                    "state": "MERGED" if payloads else "OPEN",
                    "isDraft": False,
                    "mergeCommit": {"oid": "b" * 40} if payloads else None,
                }
            )
        if input_text is not None:
            payloads.append(json.loads(input_text))
            return '{"merged":true}'
        if argv[:2] == ("gh", "api"):
            return json.dumps({"sha": "b" * 40, "message": message, "parents": [{"sha": "c" * 40}]})
        raise AssertionError(argv)

    monkeypatch.setattr(workflow, "run", remote)
    result = workflow.merge_pr(Path("/unused"), 12)
    assert result["status"] == "merged_verified"
    assert result["merge_sha"] == "b" * 40
    assert payloads == [
        {
            "merge_method": "squash",
            "sha": "a" * 40,
            "commit_title": "feat: example (#12)",
            "commit_message": message.split("\n\n", 1)[1] + "\n",
        }
    ]
    assert calls[-1][-1] == "repos/owner/project/git/commits/" + "b" * 40


def test_cli_refuses_repository_context_outside_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    def remote(root: Path, *argv: str, capture: bool = False, input_text: str | None = None) -> str:
        if argv[:3] == ("git", "remote", "get-url"):
            return "https://github.com/owner/other.git"
        if argv[:3] == ("gh", "repo", "view"):
            return '{"nameWithOwner":"owner/project","url":"https://github.com/owner/project"}'
        raise AssertionError("No remote write allowed")

    monkeypatch.setattr(workflow, "run", remote)
    with pytest.raises(ValueError, match="origin"):
        workflow.merge_pr(Path("/unused"), 12)


def test_cli_plan_failure_uses_controlled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def repository(_root: Path) -> str:
        return "owner/project"

    def fail(root: Path, *argv: str, capture: bool = False, input_text: str | None = None) -> str:
        raise OSError("external diagnostics")

    monkeypatch.setattr(workflow, "_merge_repository", repository)
    monkeypatch.setattr(workflow, "run", fail)
    with pytest.raises(ValueError, match="Cannot observe"):
        workflow.pr_merge_plan(Path("/unused"), 12)


def test_github_setup_keeps_writer_bypass_separate_from_quality_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, object]] = []

    def remote(root: Path, *argv: str, capture: bool = False, input_text: str | None = None) -> str:
        if argv[:3] == ("gh", "repo", "view"):
            return '{"nameWithOwner":"owner/project","defaultBranchRef":{"name":"main"}}'
        if argv[:2] == ("gh", "api") and "actions/runs?" in argv[2]:
            return '{"workflow_runs":[{"check_suite_id":42}]}'
        if argv[:2] == ("gh", "api") and "check-suites/" in argv[2]:
            return '{"app":{"slug":"github-actions","id":9876}}'
        if argv[:2] == ("gh", "api") and "/rulesets?" in argv[2] and input_text is None:
            return "[]"
        if input_text is not None:
            payloads.append(json.loads(input_text))
        return ""

    monkeypatch.setattr(workflow, "run", remote)
    workflow.configure_github(Path("/unused"))
    assert len(payloads) == 2
    assert payloads[0]["bypass_actors"] == []
    assert payloads[1]["bypass_actors"] == [{"actor_id": 9876, "actor_type": "Integration", "bypass_mode": "always"}]
    from typing import cast

    gate_rules = cast(list[object], payloads[0]["rules"])
    assert {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": "Project quality", "integration_id": 9876}],
        },
    } in gate_rules
    assert payloads[1]["rules"] == [{"type": "update", "parameters": {"update_allows_fetch_and_merge": False}}]
