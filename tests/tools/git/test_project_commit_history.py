"""Complete-SHA history listing for reconstruction; real Git ordering and schema."""

import asyncio
from pathlib import Path

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration


def test_history_returns_ordered_commits_with_parent_links(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await git(tmp_path, "branch", "-M", "main")
        (tmp_path / "base.txt").write_text("base\n")
        await git(tmp_path, "add", "base.txt")
        await git(tmp_path, "commit", "-m", "chore: initial")
        base = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "-c", "feat/first")

        (tmp_path / "first.txt").write_text("first\n")
        await git(tmp_path, "add", "first.txt")
        await git(tmp_path, "commit", "-m", "feat(core): first capability")
        first = await git(tmp_path, "rev-parse", "HEAD")
        (tmp_path / "second.txt").write_text("second\n")
        await git(tmp_path, "add", "second.txt")
        await git(tmp_path, "commit", "-m", "fix(core): follow-up")
        second = await git(tmp_path, "rev-parse", "HEAD")

        async with client(tmp_path) as session:
            request: dict[str, object] = {
                "base_ref": "main",
                "target_ref": "feat/first",
                "expected_base_head": base,
                "expected_target_head": second,
            }
            result = await success(session, "inspect_project_commit_history", request)
            assert result["base_head"] == base and result["target_head"] == second
            assert result["count"] == 2
            assert result["commits"] == [
                {"sha": first, "parents": [base], "title": "feat(core): first capability"},
                {"sha": second, "parents": [first], "title": "fix(core): follow-up"},
            ]
            assert "password" not in str(result)
            await failure(session, "inspect_project_commit_history", {**request, "expected_base_head": second})
            await failure(
                session,
                "inspect_project_commit_history",
                {
                    **request,
                    "base_ref": "feat/first",
                    "target_ref": "main",
                    "expected_base_head": second,
                    "expected_target_head": base,
                },
            )
        assert await git(tmp_path, "rev-parse", "HEAD") == second

    asyncio.run(scenario())
