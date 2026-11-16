"""Exercise PR collection against real Git topology without GitHub credentials."""

from pathlib import Path

import pytest

from local_dev_mcp.tools import workflow_pr
from tests.support.git import git_sync

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("hostile_global", [False, True], ids=["normal-git", "hostile-global"])
def test_real_git_history_collects_only_new_assessments_and_exact_commits(
    tmp_path: Path, hostile_global: bool, request: pytest.FixtureRequest
) -> None:
    if hostile_global:
        request.getfixturevalue("hostile_git_global")

    def git(*args: str) -> str:
        return git_sync(tmp_path, *args)

    def save(path: str, content: str) -> None:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    git("init", "-q")
    git("config", "user.name", "PR Test")
    git("config", "user.email", "pr-test@example.invalid")
    git("checkout", "-qb", "main")
    save(
        ".changes/preexisting.toml",
        'impact = "major"\nsummary = "Old change"\nreason = "Old reason"\nmigration = ""\n',
    )
    save("base.txt", "base\n")
    git("add", ".")
    git("commit", "-qm", "chore: create baseline")
    baseline = git("rev-parse", "HEAD").strip()

    git("checkout", "-qb", "feat/example")
    save(
        ".changes/new-feature.toml",
        'impact = "minor"\nsummary = "New change"\nreason = "New reason"\nmigration = ""\n',
    )
    save("feature.txt", "feature\n")
    git("add", ".")
    git("commit", "-qm", "feat: add new feature")
    first = git("rev-parse", "HEAD").strip()
    save("feature.txt", "feature\nsecond change\n")
    git("commit", "-qam", "test: cover new feature")
    last = git("rev-parse", "HEAD").strip()

    def command(root: Path, *args: str, capture: bool = False, input_text: str | None = None) -> str:
        del root, capture, input_text
        if args[:3] == ("gh", "repo", "view"):
            return '{"url":"https://github.com/example/project"}'
        if args[:2] == ("gh", "api"):
            return '{"object":{"sha":"' + baseline + '"}}'
        return git(*args[1:])

    result = workflow_pr.collect(tmp_path, command, "main", "HEAD")
    assert result["base"] == baseline
    assert result["head"] == last
    assert [entry["sha"] for entry in result["commits"]] == [first, last]
    assert [entry["id"] for entry in result["assessments"]] == ["new-feature"]
    assert "preexisting" not in str(result)
