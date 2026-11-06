"""PR metadata renderer and GitHub sync guards are deterministic and fail-closed."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from local_dev_mcp.tools import workflow_pr

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
A = "a" * 40
B = "b" * 40
C = "c" * 40


def facts(commits: int = 3) -> dict[str, Any]:
    return {
        "schema": 1,
        "repo_url": "https://github.com/example/project",
        "base": A,
        "merge_base": A,
        "head": B,
        "commits": [{"sha": chr(98 + i) * 40, "title": "feat: step " + str(i)} for i in range(commits)],
        "assessments": [
            {
                "id": "workflow-mvp",
                "impact": "minor",
                "summary": "Add guarded PR integration",
                "reason": "Add an optional PR integration CLI with unchanged MCP inputs.",
                "migration": "",
            },
            {
                "id": "project-owned-hooks",
                "impact": "patch",
                "summary": "Activate project-owned hooks",
                "reason": "Restore local Hook functionality.",
                "migration": "",
            },
        ],
    }


def test_render_uses_all_short_commit_links_and_new_assessments() -> None:
    result = workflow_pr.render(ROOT, facts())
    assert result.count("/commit/") == 3
    assert "https://github.com/example/project/commit/" + B in result
    assert "## Release impact" in result
    assert "workflow-mvp" in result
    assert "project-owned-hooks" in result
    assert "Add an optional PR integration CLI" in result
    assert "Restore local Hook functionality" not in result
    assert "## Activation" not in result
    assert "Unit 494" not in result
    assert workflow_pr.parse(result)["head"] == B


def test_large_commit_history_collapses_to_link_after_pr_number() -> None:
    result = workflow_pr.render(ROOT, facts(6))
    assert "6 commits" in result
    assert "/commit/" not in result
    assert "/pull/" not in result
    linked = workflow_pr.render(ROOT, facts(6), number=42)
    assert "/pull/42/commits" in linked
    assert "/commit/" not in linked


def test_reviewed_exceptions_require_evidence_but_display_only_relevant_details() -> None:
    context = {
        "purpose": "Reconcile public and development PR histories.",
        "exceptions": [
            {
                "type": "Activation",
                "detail": "Enable remote rules only after initial integration.",
                "evidence": "docs/adr/0006-native-pr-workflow-mvp.md",
            }
        ],
    }
    body = workflow_pr.render(ROOT, facts(), context)
    assert "Reconcile public and development PR histories." in body
    assert "## Activation" in body
    assert "Enable remote rules" in body
    assert "docs/adr/0006" not in body
    assert "## Verification gap" not in body
    assert workflow_pr.parse(body)["head"] == B


@pytest.mark.parametrize(
    "context",
    [
        {"exceptions": [{"type": "Arbitrary note", "detail": "x", "evidence": "doc"}]},
        {"exceptions": [{"type": "Activation", "detail": "x"}]},
        {"purpose": ""},
        {"extra": "unexpected"},
    ],
)
def test_review_context_rejects_unsupported_claims(context: object) -> None:
    with pytest.raises(ValueError):
        workflow_pr.render(ROOT, facts(), context)


def test_migration_is_only_rendered_when_assessment_has_text() -> None:
    data = facts()
    data["assessments"][0]["migration"] = "Restart service after upgrade."
    body = workflow_pr.render(ROOT, data)
    assert "## Migration" in body
    assert "Restart service after upgrade." in body


def test_generated_region_tampering_refuses_refresh() -> None:
    old = workflow_pr.render(ROOT, facts())
    tampered = old.replace("Add guarded PR integration", "Unreviewed rewrite")
    with pytest.raises(ValueError, match="externally changed"):
        workflow_pr.refresh(ROOT, facts(), tampered, 42)


def test_manual_regions_remain_exact_after_regeneration() -> None:
    old = workflow_pr.render(
        ROOT,
        facts(),
        {
            "purpose": "Manually reviewed purpose.\n\nSecond paragraph.",
            "exceptions": [{"type": "Activation", "detail": "Run setup only after release.", "evidence": "ADR 6"}],
        },
    )
    changed = deepcopy(facts())
    changed["assessments"][0]["summary"] = "A new machine-generated summary"
    updated = workflow_pr.refresh(ROOT, changed, old, 45)
    original = workflow_pr.parse(old)
    refreshed = workflow_pr.parse(updated)
    assert refreshed["purpose"] == original["purpose"]
    assert refreshed["exceptions"] == original["exceptions"]
    assert "A new machine-generated summary" in updated
    assert workflow_pr.parse(updated)["head"] == B


def test_head_change_requires_explicit_manual_review() -> None:
    old = workflow_pr.render(ROOT, facts())
    new = deepcopy(facts())
    new["head"] = C
    with pytest.raises(ValueError, match="HEAD changed"):
        workflow_pr.refresh(ROOT, new, old, 12)
    result = workflow_pr.refresh(ROOT, new, old, 12, accept_new_head=True)
    assert workflow_pr.parse(result)["head"] == C
    assert workflow_pr.parse(result)["purpose"] == workflow_pr.parse(old)["purpose"]


def test_missing_or_duplicated_markers_are_rejected() -> None:
    body = workflow_pr.render(ROOT, facts())
    with pytest.raises(ValueError, match="markers"):
        workflow_pr.parse(body.replace("<!-- pr-body:exceptions:end -->", ""))
    with pytest.raises(ValueError, match="markers"):
        workflow_pr.parse(body + "<!-- pr-body:purpose:start -->")


def test_collect_uses_merge_base_and_only_added_assessments() -> None:
    calls: list[tuple[str, ...]] = []
    toml = 'impact = "minor"\nsummary = "New feature"\nreason = "A reason."\nmigration = ""\n'

    def command(_root: Path, *args: str, capture: bool = False, input_text: str | None = None) -> str:
        del capture, input_text
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "--verify"):
            return (A if args[-1].startswith("main") else B) + "\n"
        if args[:2] == ("git", "merge-base"):
            return A + "\n"
        if args[:2] == ("git", "log"):
            return B + "\x00feat: test\x00"
        if args[:2] == ("git", "diff"):
            return ".changes/new-feature.toml\x00"
        if args[:2] == ("git", "show"):
            return toml
        if args[:3] == ("gh", "repo", "view"):
            return '{"url":"https://github.com/example/project"}'
        if args[:2] == ("gh", "api"):
            return json.dumps({"object": {"sha": A}})
        raise AssertionError(args)

    collected = workflow_pr.collect(ROOT, command, "main", "HEAD")
    assert collected["head"] == B
    assert collected["merge_base"] == A
    assert collected["assessments"][0]["id"] == "new-feature"
    assert len(collected["commits"]) == 1
    assert all("legacy" not in str(x) for x in calls)


def test_collect_fails_when_no_new_assessments() -> None:
    def command(_root: Path, *args: str, capture: bool = False, input_text: str | None = None) -> str:
        del capture, input_text
        if args[:3] == ("git", "rev-parse", "--verify"):
            return (A if args[-1].startswith("main") else B) + "\n"
        if args[:2] == ("git", "merge-base"):
            return A
        if args[:2] == ("git", "log"):
            return B + "\x00feat: test\x00"
        if args[:2] == ("git", "diff"):
            return ""
        if args[:3] == ("gh", "repo", "view"):
            return '{"url":"https://github.com/example/project"}'
        if args[:2] == ("gh", "api"):
            return json.dumps({"object": {"sha": A}})
        raise AssertionError(args)

    with pytest.raises(ValueError, match="explicit"):
        workflow_pr.collect(ROOT, command, "main", "HEAD")


def test_sync_checks_head_base_remote_and_preserves_manual_content(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = workflow_pr.render(ROOT, facts())
    meta = {
        "body": initial,
        "headRefOid": B,
        "baseRefOid": A,
        "baseRefName": "main",
        "state": "OPEN",
        "url": "https://github.com/example/project/pull/42",
    }
    records: list[tuple[str, ...]] = []

    def command(_root: Path, *args: str, capture: bool = False, input_text: str | None = None) -> str:
        del capture
        records.append(args)
        if args[:3] == ("gh", "pr", "view"):
            return json.dumps(meta)
        if args[:3] == ("gh", "pr", "edit"):
            assert input_text is not None
            assert "/pull/42" not in input_text or "Full history" in input_text
            return ""
        raise AssertionError(args)

    changed = facts()
    changed["assessments"][0]["summary"] = "Updated generated metadata"

    def collected(*_args: object) -> dict[str, Any]:
        return changed

    monkeypatch.setattr(workflow_pr, "collect", collected)
    result = workflow_pr.sync(ROOT, command, 42, "main")
    assert workflow_pr.parse(result)["head"] == B
    assert any(args[:3] == ("gh", "pr", "edit") for args in records)

    meta["baseRefOid"] = C
    with pytest.raises(ValueError, match="Local refs differ"):
        workflow_pr.sync(ROOT, command, 42, "main")


def test_render_escapes_markup_in_untrusted_commit_subjects() -> None:
    data = facts()
    data["commits"][0]["title"] = "feat: [outside](https://example.invalid)"
    data["assessments"][0]["summary"] = "Add [outside](https://example.invalid)"
    body = workflow_pr.render(ROOT, data)
    assert r"\[outside\]" in body
    assert "[outside](https://example.invalid)" not in body


def test_cli_render_entrypoint_writes_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from local_dev_mcp.tools import workflow

    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(facts()), encoding="utf-8")
    code = workflow.main(["--workspace", str(ROOT), "pr-body", "render", "--input", str(snapshot)])
    assert code == 0
    assert "## Release impact" in capsys.readouterr().out
