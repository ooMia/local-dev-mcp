"""Worktrunk cleanup must serialize with registered checks on the same server."""

import asyncio
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import JobDefinition
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


@pytest.mark.skipif(shutil.which("wt") is None, reason="Worktrunk CLI unavailable")
def test_cleanup_refuses_running_worktrunk_check_and_preserves_source(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        cfg = replace(
            policy(tmp_path),
            allowed_commands=("git", "wt", "git.worktree.write", "task.slow"),
            jobs=(
                JobDefinition(
                    "slow",
                    "lint",
                    "task.slow",
                    (sys.executable, "-c", "import time; time.sleep(6)"),
                ),
            ),
        )
        async with Client(create_server(cfg)) as client:
            created = await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/wt-busy",
                    "expected_branch": "main",
                    "expected_head": base,
                    "expected_base_head": base,
                },
            )
            target = tmp_path / str(created["path"])
            started = await success(
                client,
                "start_project_worktree_task",
                {
                    "task": "slow",
                    "request_id": "busy-wt-test",
                    "branch": "feat/wt-busy",
                    "expected_branch_head": base,
                    "expected_branch": "main",
                    "expected_head": base,
                    "backend": "worktrunk",
                },
            )
            assert started["state"] == "running"
            denied = await failure(
                client,
                "remove_project_worktrunk",
                {"branch": "feat/wt-busy", "expected_branch_head": base},
            )
            assert "task" in denied.lower() and ("running" in denied.lower() or "active" in denied.lower())
            assert target.is_dir()
            assert await git(target, "rev-parse", "HEAD") == base
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            cancelled = await success(client, "cancel_project_task", {"job_id": started["job_id"]})
            assert cancelled["state"] in ("cancelled", "cleanup_unconfirmed")

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked"])
def test_cleanup_refuses_dirty_checkout_before_worktrunk_invocation(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async def scenario() -> None:
        from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter

        base, _ = await seed(tmp_path, workspace_factory)
        target = tmp_path / ".lab" / "candidate"
        await git(tmp_path, "worktree", "add", str(target), "feat/publish")
        if change == "untracked":
            (target / "untracked.txt").write_text("keep me\n")
        else:
            (target / "base.txt").write_text("keep me\n")
            if change == "staged":
                await git(target, "add", "base.txt")

        invoked = False

        async def forbidden_remove(self: WorktrunkAdapter, *argv: str, write: bool) -> dict[str, object]:
            nonlocal invoked
            invoked = True
            return {"branch": "feat/publish", "path": str(target), "branch_outcome": "not_attempted"}

        monkeypatch.setattr(WorktrunkAdapter, "_run", forbidden_remove)
        cfg = replace(policy(tmp_path), allowed_commands=("git", "wt", "git.worktree.write"))
        async with Client(create_server(cfg)) as client:
            await failure(
                client,
                "remove_project_worktrunk",
                {"branch": "feat/publish", "expected_branch_head": base, "delete_branch": False},
            )
        assert not invoked
        assert target.is_dir()
        assert await git(target, "rev-parse", "HEAD") == base
        assert await git(tmp_path, "rev-parse", "HEAD") == base
        if change == "untracked":
            assert (target / "untracked.txt").read_text() == "keep me\n"
        else:
            assert (target / "base.txt").read_text() == "keep me\n"

    asyncio.run(scenario())


def test_cleanup_rejects_checkout_dirtied_during_final_readiness(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter

        base, _ = await seed(tmp_path, workspace_factory)
        target = tmp_path / ".lab" / "racing"
        await git(tmp_path, "worktree", "add", str(target), "feat/publish")
        original = WorktrunkAdapter.inspect
        inspected = 0
        invoked = False

        async def inspect_then_dirty(self: WorktrunkAdapter, branch: str) -> dict[str, object]:
            nonlocal inspected
            result = await original(self, branch)
            inspected += 1
            if inspected == 1:
                assert result["state"] == "ready"
                (target / "base.txt").write_text("concurrent edit\n")
            return result

        async def forbidden_remove(self: WorktrunkAdapter, *argv: str, write: bool) -> dict[str, object]:
            nonlocal invoked
            invoked = True
            return {"branch": "feat/publish", "path": str(target), "branch_outcome": "not_attempted"}

        monkeypatch.setattr(WorktrunkAdapter, "inspect", inspect_then_dirty)
        monkeypatch.setattr(WorktrunkAdapter, "_run", forbidden_remove)
        cfg = replace(policy(tmp_path), allowed_commands=("git", "wt", "git.worktree.write"))
        async with Client(create_server(cfg)) as client:
            await failure(
                client,
                "remove_project_worktrunk",
                {"branch": "feat/publish", "expected_branch_head": base, "delete_branch": False},
            )
        assert inspected == 1
        assert not invoked
        assert target.is_dir()
        assert (target / "base.txt").read_text() == "concurrent edit\n"

    asyncio.run(scenario())


def test_cleanup_rejects_checkout_moved_after_registration(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter

        base, _ = await seed(tmp_path, workspace_factory)
        target = tmp_path / ".lab" / "original"
        moved = tmp_path / ".lab" / "moved"
        await git(tmp_path, "worktree", "add", str(target), "feat/publish")
        original = WorktrunkAdapter.inspect
        invoked = False
        inspected = 0

        async def inspect_moved(self: WorktrunkAdapter, branch: str) -> dict[str, object]:
            nonlocal inspected
            inspected += 1
            if inspected == 1:
                await git(tmp_path, "worktree", "move", str(target), str(moved))
            return await original(self, branch)

        async def forbidden_remove(self: WorktrunkAdapter, *argv: str, write: bool) -> dict[str, object]:
            nonlocal invoked
            invoked = True
            return {"branch": "feat/publish", "path": str(target), "branch_outcome": "not_attempted"}

        monkeypatch.setattr(WorktrunkAdapter, "inspect", inspect_moved)
        monkeypatch.setattr(WorktrunkAdapter, "_run", forbidden_remove)
        cfg = replace(policy(tmp_path), allowed_commands=("git", "wt", "git.worktree.write"))
        async with Client(create_server(cfg)) as client:
            await failure(
                client,
                "remove_project_worktrunk",
                {"branch": "feat/publish", "expected_branch_head": base, "delete_branch": False},
            )
        assert inspected == 1
        assert not invoked
        assert not target.exists()
        assert moved.is_dir()
        assert await git(moved, "rev-parse", "HEAD") == base

    asyncio.run(scenario())
