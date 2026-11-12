"""Main checkout setup must refuse stale refs, foreign registration and occupied paths."""

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure
from tests.support.worktrees import policy, seed

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("wt") is None, reason="Worktrunk CLI unavailable on macOS"),
]


def _policy(root: Path) -> Policy:
    state = policy(root)
    return replace(state, allowed_commands=(*state.allowed_commands, "wt"))


def test_existing_main_checkout_is_not_adopted(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/controller")
        occupied = tmp_path / ".lab" / "other-main"
        occupied.parent.mkdir(parents=True, exist_ok=True)
        await git(tmp_path, "worktree", "add", str(occupied), "main")
        before = await git(tmp_path, "worktree", "list", "--porcelain")

        async with Client(create_server(_policy(tmp_path))) as client:
            message = await failure(
                client,
                "prepare_project_worktrunk_main",
                {"expected_branch": "feat/controller", "expected_head": base, "expected_main_head": base},
            )
            assert "already checked out" in message

        assert before == await git(tmp_path, "worktree", "list", "--porcelain")
        assert await git(occupied, "rev-parse", "HEAD") == base
        assert not (tmp_path / ".lab" / "wt" / "main").exists()
        assert await git(tmp_path, "branch", "--show-current") == "feat/controller"

    asyncio.run(scenario())


def test_moved_main_ref_is_rejected_without_creating_checkout(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/controller")
        (tmp_path / "feature.txt").write_text("updated\n")
        await git(tmp_path, "add", "feature.txt")
        await git(tmp_path, "commit", "-m", "feat: advance ref")
        changed = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "branch", "-f", "main", changed)

        async with Client(create_server(_policy(tmp_path))) as client:
            message = await failure(
                client,
                "prepare_project_worktrunk_main",
                {"expected_branch": "feat/controller", "expected_head": changed, "expected_main_head": base},
            )
            assert "Main changed" in message

        assert await git(tmp_path, "rev-parse", "main") == changed
        assert await git(tmp_path, "rev-parse", "HEAD") == changed
        assert not (tmp_path / ".lab" / "wt" / "main").exists()

    asyncio.run(scenario())


def test_symlink_main_path_is_not_followed_or_deleted(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/controller")
        destination = tmp_path / "user-directory"
        destination.mkdir()
        (destination / "sentinel").write_text("keep original bytes")
        path = tmp_path / ".lab" / "wt" / "main"
        path.parent.mkdir(parents=True)
        path.symlink_to(destination, target_is_directory=True)

        async with Client(create_server(_policy(tmp_path))) as client:
            message = await failure(
                client,
                "prepare_project_worktrunk_main",
                {"expected_branch": "feat/controller", "expected_head": base, "expected_main_head": base},
            )
            assert "symlink" in message

        assert path.is_symlink()
        assert (destination / "sentinel").read_text() == "keep original bytes"
        assert await git(tmp_path, "rev-parse", "main") == base
        assert await git(tmp_path, "branch", "--show-current") == "feat/controller"

    asyncio.run(scenario())
