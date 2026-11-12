"""Worktrunk proposal-only MCP and independent Git fast-forward contracts."""

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


def _policy(root: Path) -> Policy:
    state = policy(root)
    return replace(state, allowed_commands=(*state.allowed_commands, "wt"))


def _wt_installed() -> None:
    if shutil.which("wt") is None:
        pytest.skip("Worktrunk CLI is unavailable on this test host")


def test_native_ff_does_not_squash_source(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    _wt_installed()

    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        adapter = WorktrunkAdapter(ServerSettings.load(_policy(tmp_path)))
        created = await adapter.create("feat/wt-ff", "main", base, "main", base)
        source = tmp_path / created["path"]
        (source / "ff.txt").write_text("new commit\n")
        await git(source, "add", "ff.txt")
        await git(source, "commit", "-m", "feat: native fast-forward")
        head = await git(source, "rev-parse", "HEAD")
        # Only a distinct, user-controlled Git command integrates main.
        assert await git(tmp_path, "rev-parse", "HEAD") == base
        await git(tmp_path, "merge", "--ff-only", "feat/wt-ff")
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        await adapter.remove("feat/wt-ff", head, delete_branch=True)
        assert not source.exists()

    asyncio.run(scenario())


def test_mcp_worktrunk_lifecycle_and_strict_authorization(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    _wt_installed()

    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(_policy(tmp_path))) as session:
            registered = {tool.name: tool for tool in await session.list_tools()}
            names = {"create_project_worktrunk", "remove_project_worktrunk"}
            assert names <= registered.keys()
            assert "merge_project_worktrunk" not in registered
            assert all(registered[name].input_schema["additionalProperties"] is False for name in names)
            assert all("run_hooks" in registered[name].input_schema["properties"] for name in names)
            await failure(session, "create_project_worktrunk", {"branch": "--force"})
            created = await success(
                session,
                "create_project_worktrunk",
                {"branch": "feat/wt-mcp", "expected_branch": "main", "expected_head": base, "expected_base_head": base},
            )
            path = tmp_path / str(created["path"])
            assert path.exists()
            (path / "mcp.txt").write_text("from client\n")
            await git(path, "add", "mcp.txt")
            await git(path, "commit", "-m", "feat: mcp bridge")
            head = await git(path, "rev-parse", "HEAD")
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            removed = await success(
                session,
                "remove_project_worktrunk",
                {"branch": "feat/wt-mcp", "expected_branch_head": head, "delete_branch": False},
            )
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            assert await git(tmp_path, "branch", "--list", "feat/wt-mcp") != ""
            assert removed["worktree_removed"] is True
            assert not path.exists()
        denied = replace(policy(tmp_path), allowed_commands=("git",))
        async with Client(create_server(denied)) as session:
            await failure(
                session,
                "create_project_worktrunk",
                {
                    "branch": "feat/blocked",
                    "expected_branch": "main",
                    "expected_head": head,
                    "expected_base_head": head,
                },
            )

    asyncio.run(scenario())
