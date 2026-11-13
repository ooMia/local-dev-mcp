"""Review committed, staged, unstaged and untracked changes without changing Git state."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


def _changes(result: dict[str, object]) -> list[dict[str, object]]:
    raw = result["committed_changes"]
    assert isinstance(raw, list)
    return cast(list[dict[str, object]], raw)


def _patches(result: dict[str, object]) -> dict[str, str]:
    raw = result["patches"]
    assert isinstance(raw, dict)
    return cast(dict[str, str], raw)


def test_review_combines_committed_and_local_changes_without_staging(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        linked = tmp_path / ".lab" / "review"
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        (linked / "committed.txt").write_text("committed-change\n")
        await git(linked, "add", "committed.txt")
        await git(linked, "commit", "-m", "feat: review source")
        head = await git(linked, "rev-parse", "HEAD")
        (linked / "staged.txt").write_text("staged-change\n")
        await git(linked, "add", "staged.txt")
        (linked / "base.txt").write_text("unstaged-change\n")
        (linked / "draft.txt").write_text("untracked-private-canary")
        state_before = await git(linked, "status", "--porcelain")
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git",)))) as client:
            result = await success(
                client,
                "get_project_worktree_review",
                {
                    "branch": "feat/publish",
                    "expected_head": head,
                    "include_patch": True,
                },
            )
            assert result["head"] == head and result["base_head"] == base
            assert result["state"] == "dirty"
            assert result["complete"] is True
            assert result["branch_only_commits"] == 1
            assert result["staged"] == 1 and result["unstaged"] == 1 and result["untracked"] == 1
            assert any(change["path"] == "committed.txt" for change in _changes(result))
            patch = _patches(result)
            assert "committed-change" in patch["committed"]
            assert "staged-change" in patch["staged"]
            assert "unstaged-change" in patch["unstaged"]
            assert "untracked-private-canary" not in str(result)
            summary = await success(
                client,
                "get_project_worktree_review",
                {
                    "branch": "feat/publish",
                    "expected_head": head,
                },
            )
            assert _patches(summary) == {"committed": "", "staged": "", "unstaged": ""}
            await failure(
                client,
                "get_project_worktree_review",
                {
                    "branch": "feat/publish",
                    "expected_head": base,
                },
            )
        assert await git(linked, "status", "--porcelain") == state_before
        assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


def test_review_excludes_protected_tracked_data(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        linked = tmp_path / ".lab" / "protected-review"
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        (linked / ".env").write_text("PROTECTED_PRIVATE_VALUE\n")
        await git(linked, "add", "-f", ".env")
        await git(linked, "commit", "-m", "test: protected fixture")
        head = await git(linked, "rev-parse", "HEAD")
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git",)))) as client:
            result = await success(
                client,
                "get_project_worktree_review",
                {
                    "branch": "feat/publish",
                    "expected_head": head,
                    "include_patch": True,
                },
            )
            assert result["complete"] is False
            assert result["reason"] == "restricted_or_unavailable_paths"
            assert "PROTECTED_PRIVATE_VALUE" not in str(result)
            assert ".env" not in str(_changes(result))
        assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


def test_review_rejects_unavailable_worktree_and_denied_path(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        linked = tmp_path / ".lab" / "denied-review"
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git",)))) as client:
            await failure(
                client,
                "get_project_worktree_review",
                {
                    "branch": "feat/publish",
                    "expected_head": head,
                },
            )
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        restricted = replace(policy(tmp_path), allowed_commands=("git",), denied_paths=(linked,))
        async with Client(create_server(restricted)) as client:
            await failure(
                client,
                "get_project_worktree_review",
                {
                    "branch": "feat/publish",
                    "expected_head": head,
                },
            )
        assert linked.is_dir()

    asyncio.run(scenario())
