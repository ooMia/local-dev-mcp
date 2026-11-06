"""The GitHub boundary emits conditional merges and composes native rules."""

import json
from pathlib import Path

import pytest

from local_dev_mcp.tools import workflow

pytestmark = pytest.mark.unit


def test_pr_merge_pins_observed_head_and_records_real_pr_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def remote(root: Path, *argv: str, capture: bool = False, input_text: str | None = None) -> str:
        calls.append(argv)
        if argv[:3] == ("gh", "pr", "view"):
            return json.dumps(
                {
                    "url": "https://github.com/owner/project/pull/12",
                    "title": "feat: example",
                    "headRefOid": "a" * 40,
                    "baseRefName": "main",
                    "state": "OPEN",
                    "isDraft": False,
                }
            )
        return ""

    monkeypatch.setattr(workflow, "run", remote)
    workflow.merge_pr(Path("/unused"), 12)
    emitted = calls[-1]
    assert emitted[:4] == ("gh", "pr", "merge", "12")
    assert "--squash" in emitted
    assert emitted[emitted.index("--match-head-commit") + 1] == "a" * 40
    assert emitted[emitted.index("--body") + 1] == (
        "Source-PR: https://github.com/owner/project/pull/12\nSource-Head: " + "a" * 40 + "\n"
    )


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
