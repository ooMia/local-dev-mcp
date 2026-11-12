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
