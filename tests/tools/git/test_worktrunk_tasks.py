"""Worktrunk tasks run through the existing scheduler in the exact target checkout."""

import asyncio
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import JobDefinition, Policy
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration

CHECK = (
    "import json; from pathlib import Path; "
    "p=Path('.lab'); p.mkdir(exist_ok=True); "
    "(p/'executed').write_text('yes'); "
    "print(json.dumps({'cwd':str(Path.cwd()),'source':Path('base.txt').read_text()}))"
)


def _policy(root: Path, *, wt: bool = True) -> Policy:
    allowed = ("git", "git.worktree.write", "task.check") + (("wt",) if wt else ())
    return replace(
        policy(root),
        allowed_commands=allowed,
        jobs=(JobDefinition("check", "lint", "task.check", (sys.executable, "-c", CHECK)),),
    )


async def _terminal(session: Client[FastMCPTransport], job_id: str) -> dict[str, object]:
    async with asyncio.timeout(15):
        while True:
            result = await success(session, "get_project_task", {"job_id": job_id, "include_output": True})
            if result["state"] != "running":
                return result
            await asyncio.sleep(0.02)


@pytest.mark.skipif(shutil.which("wt") is None, reason="macOS Worktrunk CLI is required")
def test_worktrunk_check_uses_exact_checkout_and_preserves_dirty_controller(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(_policy(tmp_path))) as client:
            created = await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/wt-check",
                    "expected_branch": "main",
                    "expected_head": base,
                    "expected_base_head": base,
                },
            )
            target = tmp_path / str(created["path"])
            (tmp_path / "base.txt").write_text("dirty controller only\n")
            args: dict[str, object] = {
                "task": "check",
                "request_id": "wt-target-check",
                "branch": "feat/wt-check",
                "expected_branch_head": base,
                "expected_branch": "main",
                "expected_head": base,
                "backend": "worktrunk",
            }
            started = await success(client, "start_project_worktree_task", args)
            result = await _terminal(client, str(started["job_id"]))
            assert result["state"] == "succeeded", result
            assert result["exit_code"] == 0
            context = cast(dict[str, object], result["worktree"])
            assert context["verified"] is True
            assert context["backend"] == "worktrunk"
            output = json.loads(str(result["stdout"]).splitlines()[0])
            assert output["cwd"] == str(target)
            assert output["source"] == "base\n"
            assert (target / ".lab/executed").read_text() == "yes"
            assert not (tmp_path / ".lab/executed").exists()
            assert (tmp_path / "base.txt").read_text() == "dirty controller only\n"
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            # Reusing a request_id with another backend must never reuse its evidence.
            changed = await failure(client, "start_project_worktree_task", {**args, "backend": "native"})
            assert "request_id" in changed

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("wt") is None, reason="macOS Worktrunk CLI is required")
def test_worktrunk_task_rejects_dirty_target_and_missing_wt_permission(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(_policy(tmp_path))) as client:
            created = await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/wt-check",
                    "expected_branch": "main",
                    "expected_head": base,
                    "expected_base_head": base,
                },
            )
        target = tmp_path / str(created["path"])
        (target / "base.txt").write_text("dirty target\n")
        args: dict[str, object] = {
            "task": "check",
            "request_id": "wt-dirty-check",
            "branch": "feat/wt-check",
            "expected_branch_head": base,
            "expected_branch": "main",
            "expected_head": base,
            "backend": "worktrunk",
        }
        async with Client(create_server(_policy(tmp_path))) as client:
            refusal = await failure(client, "start_project_worktree_task", args)
            assert "dirty" in refusal or "changed" in refusal
            assert not (target / ".lab/executed").exists()
        (target / "base.txt").write_text("base\n")
        async with Client(create_server(_policy(tmp_path, wt=False))) as client:
            refusal = await failure(client, "start_project_worktree_task", {**args, "request_id": "wt-denied"})
            assert "not allowed" in refusal or "permission" in refusal
            assert not (target / ".lab/executed").exists()

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("wt") is None, reason="Worktrunk CLI is required")
def test_worktrunk_task_reuses_checkout_created_by_git(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        external = tmp_path / ".lab" / "external-task"
        await git(tmp_path, "worktree", "add", str(external), "feat/publish")
        async with Client(create_server(_policy(tmp_path))) as client:
            reused = await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/publish",
                    "expected_branch": "main",
                    "expected_head": head,
                    "expected_branch_head": head,
                },
            )
            assert reused["action"] == "reused"
            assert tmp_path / str(reused["path"]) == external
            args: dict[str, object] = {
                "task": "check",
                "request_id": "external-task-check",
                "branch": "feat/publish",
                "expected_branch_head": head,
                "expected_branch": "main",
                "expected_head": head,
                "backend": "worktrunk",
            }
            started = await success(client, "start_project_worktree_task", args)
            result = await _terminal(client, str(started["job_id"]))
            assert result["state"] == "succeeded", result
            context = cast(dict[str, object], result["worktree"])
            assert context["verified"] is True
            assert context["path"] == str(external.relative_to(tmp_path))
            assert (external / ".lab/executed").read_text() == "yes"
            (external / "base.txt").write_text("dirty\n")
            refusal = await failure(
                client, "start_project_worktree_task", {**args, "request_id": "external-dirty-check"}
            )
            assert "dirty" in refusal or "changed" in refusal
            assert (external / "base.txt").read_text() == "dirty\n"

    asyncio.run(scenario())


def test_worktrunk_job_detects_controller_content_mutation_without_status_change(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        controller_file = tmp_path / "base.txt"
        controller_file.write_text("before\n")
        status_before = await git(tmp_path, "status", "--porcelain")
        assert status_before

        external = tmp_path / ".lab" / "external-task"
        await git(tmp_path, "worktree", "add", str(external), "feat/publish")
        script = f"from pathlib import Path; Path({str(controller_file)!r}).write_text('after!\\n')"
        config = replace(
            _policy(tmp_path),
            jobs=(JobDefinition("check", "lint", "task.check", (sys.executable, "-c", script)),),
        )
        async with Client(create_server(config)) as client:
            started = await success(
                client,
                "start_project_worktree_task",
                {
                    "task": "check",
                    "request_id": "controller-mutated-by-worktrunk-task",
                    "branch": "feat/publish",
                    "expected_branch_head": head,
                    "expected_branch": "main",
                    "expected_head": head,
                    "backend": "worktrunk",
                },
            )
            result = await _terminal(client, str(started["job_id"]))
            assert result["state"] == "failed", result
            assert result["exit_code"] == 0
            context = cast(dict[str, object], result["worktree"])
            assert context["backend"] == "worktrunk"
            assert context["verified"] is False

        assert controller_file.read_text() == "after!\n"
        assert await git(tmp_path, "status", "--porcelain") == status_before
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())
