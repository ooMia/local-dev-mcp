"""Prepare an explicitly scoped main checkout without switching the controller."""

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


def _policy(root: Path) -> Policy:
    state = policy(root)
    return replace(state, allowed_commands=(*state.allowed_commands, "wt"))


@pytest.mark.skipif(shutil.which("wt") is None, reason="Worktrunk CLI unavailable on macOS")
def test_prepare_main_worktree_preserves_dirty_controller(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/controller")
        (tmp_path / "base.txt").write_text("dirty original\n")
        original = (tmp_path / "base.txt").read_text()
        async with Client(create_server(_policy(tmp_path))) as client:
            result = await success(
                client,
                "prepare_project_worktrunk_main",
                {
                    "expected_branch": "feat/controller",
                    "expected_head": base,
                    "expected_main_head": base,
                },
            )
            path = tmp_path / str(result["path"])
            assert path.is_dir()
            assert await git(path, "branch", "--show-current") == "main"
            assert await git(path, "rev-parse", "HEAD") == base
            assert await git(tmp_path, "branch", "--show-current") == "feat/controller"
            assert (tmp_path / "base.txt").read_text() == original
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            rejected = await failure(
                client,
                "prepare_project_worktrunk_main",
                {
                    "expected_branch": "feat/controller",
                    "expected_head": base,
                    "expected_main_head": base,
                },
            )
            assert "checked out" in rejected or "already" in rejected or "occupied" in rejected
            assert (tmp_path / "base.txt").read_text() == original

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("wt") is None, reason="Worktrunk CLI unavailable on macOS")
def test_prepare_main_refuses_occupied_path_and_preserves_user_files(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/controller")
        path = tmp_path / ".lab" / "wt" / "main"
        path.mkdir(parents=True)
        (path / "sentinel.txt").write_text("do not touch")
        async with Client(create_server(_policy(tmp_path))) as client:
            rejection = await failure(
                client,
                "prepare_project_worktrunk_main",
                {"expected_branch": "feat/controller", "expected_head": base, "expected_main_head": base},
            )
            assert "occupied" in rejection
            assert (path / "sentinel.txt").read_text() == "do not touch"
            assert await git(tmp_path, "branch", "--show-current") == "feat/controller"

    asyncio.run(scenario())
