"""Managed worktree checks may preserve an observed dirty controller without publishing it."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import JobDefinition, Policy
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, request, seed

pytestmark = pytest.mark.integration


def _task_policy(root: Path, code: str) -> Policy:
    return replace(
        policy(root),
        allowed_commands=("git", "git.worktree.write", "task.check"),
        jobs=(JobDefinition("check", "lint", "task.check", (sys.executable, "-c", code)),),
    )


async def _terminal(session: Client[FastMCPTransport], job_id: str) -> dict[str, object]:
    async with asyncio.timeout(10):
        while True:
            result = await success(session, "get_project_task", {"job_id": job_id, "include_output": True})
            if result["state"] != "running":
                return result
            await asyncio.sleep(0.02)


def test_prepare_preserves_preexisting_tracked_staged_and_untracked_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        (tmp_path / "base.txt").write_text("staged from user\n")
        await git(tmp_path, "add", "base.txt")
        (tmp_path / "note.txt").write_text("user local draft\n")
        before = await git(tmp_path, "status", "--porcelain")
        index = await git(tmp_path, "ls-files", "--stage")
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            assert ready["state"] == "ready" and ready["clean"] is True
            target = tmp_path / str(ready["path"])
            assert (target / "base.txt").read_text() == "base\n"
        assert await git(tmp_path, "status", "--porcelain") == before
        assert await git(tmp_path, "ls-files", "--stage") == index
        assert (tmp_path / "base.txt").read_text() == "staged from user\n"
        assert (tmp_path / "note.txt").read_text() == "user local draft\n"

    asyncio.run(scenario())


def test_worktree_job_accepts_unchanged_dirty_controller(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        (tmp_path / "base.txt").write_text("user draft\n")
        (tmp_path / "note.txt").write_text("untracked\n")
        before = await git(tmp_path, "status", "--porcelain")
        async with Client(
            create_server(_task_policy(tmp_path, "from pathlib import Path; print(Path.cwd())"))
        ) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            started = await success(
                session, "start_project_worktree_task", {**request(head), "task": "check", "request_id": "dirty-pass"}
            )
            result = await _terminal(session, str(started["job_id"]))
            assert result["state"] == "succeeded" and result["exit_code"] == 0
            assert str(target) in str(result["stdout"])
            assert isinstance(result["worktree"], dict)
            assert result["worktree"]["verified"] is True
        assert await git(tmp_path, "status", "--porcelain") == before

    asyncio.run(scenario())


def test_same_status_controller_content_change_invalidates_verification(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        original = tmp_path / "base.txt"
        original.write_text("before\n")
        script = f"from pathlib import Path; Path({str(original)!r}).write_text('after!\\n')"
        async with Client(create_server(_task_policy(tmp_path, script))) as session:
            await success(session, "prepare_project_worktree", request(head))
            started = await success(
                session,
                "start_project_worktree_task",
                {**request(head), "task": "check", "request_id": "dirty-changed"},
            )
            result = await _terminal(session, str(started["job_id"]))
            assert result["state"] == "failed" and result["exit_code"] == 0
            assert isinstance(result["worktree"], dict)
            assert result["worktree"]["verified"] is False
            assert original.read_text() == "after!\n"

    asyncio.run(scenario())


def test_denied_dirty_file_blocks_worktree_preparation(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        (tmp_path / "untracked.txt").write_text("private synthetic data\n")
        denied = replace(policy(tmp_path), denied_paths=(tmp_path / "untracked.txt",))
        async with Client(create_server(denied)) as session:
            await failure(session, "prepare_project_worktree", request(head))
            state = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert state["state"] == "absent"
        assert (tmp_path / "untracked.txt").read_text() == "private synthetic data\n"

    asyncio.run(scenario())


def test_assessment_writes_still_require_clean_controller(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            await success(session, "prepare_project_worktree", request(head))
            (tmp_path / "base.txt").write_text("uncommitted user changes\n")
            assessment = (
                'impact = "minor"\nsummary = "Synthetic assessment"\n'
                'reason = "Synthetic regression check."\ncontract = "MCP"\n'
                'assessed_by = "agent"\nmigration = ""\nsupersedes = []\n'
            )
            await failure(
                session,
                "prepare_project_worktree_assessment",
                {**request(head), "assessment_id": "dirty-original", "assessment_toml": assessment},
            )
        assert (tmp_path / "base.txt").read_text() == "uncommitted user changes\n"

    asyncio.run(scenario())
