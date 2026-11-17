"""Managed worktree checks through the shared real-process task scheduler."""

import asyncio
import json
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
START = "start_project_worktree_task"
CODE = (
    "import os,sys,time,json; from pathlib import Path; "
    "p=Path('.lab'); p.mkdir(exist_ok=True); "
    "calls=p/'calls'; calls.write_text(str(int(calls.read_text())+1) if calls.exists() else '1'); "
    "print(json.dumps({'cwd':str(Path.cwd()),'label':os.getenv('TASK_LABEL'),'targets':sys.argv[1:]}),flush=True); "
    "print(os.getenv('TASK_SECRET',''),flush=True); "
    "mode=os.getenv('CHECK_MODE','check'); "
    "(p/'ready').touch(); "
    "exec(\"while mode=='wait' and not (p/'release').exists(): time.sleep(.01)\"); "
    "exec(\"if mode=='dirty': Path('base.txt').write_text('changed')\"); "
    "exec(\"if mode=='controller': Path(os.environ['CONTROLLER_BASE']).write_text('changed')\")"
)


def task_policy(root: Path) -> Policy:
    return replace(
        policy(root),
        allowed_commands=("git", "git.worktree.write", "task.check", "task.unit", "task.format"),
        public_environment=("TASK_LABEL", "CHECK_MODE", "CONTROLLER_BASE"),
        jobs=(
            JobDefinition("check", "lint", "task.check", (sys.executable, "-c", CODE)),
            JobDefinition(
                "unit", "test", "task.unit", (sys.executable, "-c", CODE), level="unit", default_targets=("base.txt",)
            ),
            JobDefinition("format", "format", "task.format", (sys.executable, "-c", CODE)),
        ),
    )


async def configure(root: Path, mode: str = "check") -> None:
    with (root / ".env").open("a") as stream:
        stream.write(
            f"\nTASK_LABEL=controller\nCHECK_MODE={mode}\nCONTROLLER_BASE={root / 'base.txt'}\n"
            "TASK_SECRET=synthetic-worktree-secret\n"
        )


async def terminal(session: Client[FastMCPTransport], job_id: str) -> dict[str, object]:
    async with asyncio.timeout(10):
        while True:
            result = await success(session, "get_project_task", {"job_id": job_id, "include_output": True})
            if result["state"] != "running":
                return result
            await asyncio.sleep(0.02)


async def marker(path: Path) -> None:
    async with asyncio.timeout(10):
        while not await asyncio.to_thread(path.exists):  # noqa: ASYNC110 - an external child process creates this filesystem marker.
            await asyncio.sleep(0.02)


def test_check_uses_target_startup_settings_and_records_pinned_context(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await configure(tmp_path)
        index = await git(tmp_path, "ls-files", "--stage")
        async with Client(create_server(task_policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            # A historical dotenv/config cannot replace startup values or recipes.
            (target / ".env").write_text("TASK_LABEL=historical\n")
            job = await success(session, START, {**request(head), "task": "check", "request_id": "worktree-check"})
            result = await terminal(session, str(job["job_id"]))
            assert result["state"] == "succeeded" and result["exit_code"] == 0
            details = json.loads(str(result["stdout"]).splitlines()[0])
            assert details["cwd"] == str(target) and details["label"] == "controller"
            assert "[REDACTED]" in str(result["stdout"]) and "synthetic-worktree-secret" not in str(result["stdout"])
            context = result["worktree"]
            assert isinstance(context, dict)
            assert context["branch"] == "feat/publish" and context["head"] == head and context["verified"] is True
            assert context["original_branch"] == "main" and context["original_head"] == head
            assert (target / ".lab/calls").read_text() == "1"
        assert not (tmp_path / ".lab/calls").exists()
        assert await git(tmp_path, "status", "--porcelain") == ""
        assert await git(tmp_path, "ls-files", "--stage") == index
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_worktree_and_controller_jobs_share_slot_and_cancellation(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await configure(tmp_path, "wait")
        async with Client(create_server(task_policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            job = await success(session, START, {**request(head), "task": "check", "request_id": "worktree-wait"})
            await marker(target / ".lab/ready")
            await failure(session, "start_project_task", {"task": "check", "request_id": "controller-blocked"})
            stopped = await success(session, "cancel_project_task", {"job_id": job["job_id"]})
            assert stopped["state"] == "cancelled" and stopped["termination_confirmed"] is True
            context = stopped["worktree"]
            assert isinstance(context, dict) and context["verified"] is True
            controller = await success(
                session, "start_project_task", {"task": "check", "request_id": "controller-wait"}
            )
            await marker(tmp_path / ".lab/ready")
            await failure(session, START, {**request(head), "task": "check", "request_id": "worktree-blocked"})
            await success(session, "cancel_project_task", {"job_id": controller["job_id"]})

    asyncio.run(scenario())


def test_completed_request_replays_after_refs_move_without_running_again(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await configure(tmp_path)
        async with Client(create_server(task_policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            args = {**request(head), "task": "check", "request_id": "stable-replay"}
            first = await success(session, START, args)
            await terminal(session, str(first["job_id"]))
            (target / "base.txt").write_text("external change")
            await git(target, "add", "base.txt")
            await git(target, "commit", "-m", "chore: advance target")
            repeated = await success(session, START, args)
            assert repeated["job_id"] == first["job_id"] and repeated["state"] == "succeeded"
            assert (target / ".lab/calls").read_text() == "1"
            await failure(session, START, {**args, "expected_branch_head": await git(target, "rev-parse", "HEAD")})
            await failure(session, "start_project_task", {"task": "check", "request_id": "stable-replay"})

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["dirty", "controller"])
def test_zero_exit_is_not_success_when_git_state_changed(
    tmp_path: Path, workspace_factory: WorkspaceFactory, mode: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await configure(tmp_path, mode)
        async with Client(create_server(task_policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            job = await success(session, START, {**request(head), "task": "check", "request_id": "changed-state"})
            result = await terminal(session, str(job["job_id"]))
            assert result["state"] == "failed" and result["exit_code"] == 0
            context = result["worktree"]
            assert isinstance(context, dict) and context["verified"] is False
            assert context["verification_error"]
        changed = tmp_path / "base.txt" if mode == "controller" else target / "base.txt"
        assert changed.read_text() == "changed"

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["stale", "format", "permission", "unknown"])
def test_invalid_worktree_checks_never_start(tmp_path: Path, workspace_factory: WorkspaceFactory, invalid: str) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await configure(tmp_path)
        selected = task_policy(tmp_path)
        async with Client(create_server(selected)) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
        target = tmp_path / str(ready["path"])
        args = {**request(head), "task": "check", "request_id": "invalid"}
        if invalid == "stale":
            args["expected_branch_head"] = "a" * 40
        elif invalid == "format":
            args["task"] = "format"
        elif invalid == "unknown":
            args["task"] = "unknown"
        else:
            selected = replace(selected, allowed_commands=("git", "git.worktree.write"))
        async with Client(create_server(selected)) as session:
            await failure(session, START, args)
        assert not (target / ".lab/calls").exists()

    asyncio.run(scenario())


def test_test_selectors_are_relative_to_target_and_physical_denials_apply(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await configure(tmp_path)
        async with Client(create_server(task_policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            args = {**request(head), "task": "unit", "request_id": "selected", "targets": ["base.txt::case"]}
            job = await success(session, START, args)
            result = await terminal(session, str(job["job_id"]))
            assert result["state"] == "succeeded"
            assert json.loads(str(result["stdout"]).splitlines()[0])["targets"] == ["base.txt::case"]
            await failure(session, START, {**args, "request_id": "escape", "targets": ["../base.txt"]})
        denied = replace(task_policy(tmp_path), denied_paths=(target / "base.txt",))
        async with Client(create_server(denied)) as session:
            await failure(session, START, {**args, "request_id": "physical-denial"})

    asyncio.run(scenario())


def test_worktree_task_schema_rejects_extra_execution_controls(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await configure(tmp_path)
        async with Client(create_server(task_policy(tmp_path))) as session:
            declared = {entry.name: entry for entry in await session.list_tools()}
            assert START in declared and declared[START].input_schema["additionalProperties"] is False
            await success(session, "prepare_project_worktree", request(head))
            args = {**request(head), "task": "check", "request_id": "strict"}
            await failure(session, START, {**args, "cwd": str(tmp_path)})
            await failure(session, START, {**args, "all_tests": "true"})

    asyncio.run(scenario())


def test_nested_recipe_uses_target_worktree_file_not_controller_copy(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await seed(tmp_path, workspace_factory)
        nested = tmp_path / "apps/server/tests"
        nested.mkdir(parents=True)
        (nested / "case.txt").write_text("controller")
        await git(tmp_path, "add", "apps/server/tests/case.txt")
        await git(tmp_path, "commit", "-m", "test: add nested workspace")
        controller_head = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "branch", "-f", "feat/publish", controller_head)
        await git(tmp_path, "switch", "feat/publish")
        (nested / "case.txt").write_text("target")
        await git(tmp_path, "add", "apps/server/tests/case.txt")
        await git(tmp_path, "commit", "-m", "test: diverge nested target")
        target_head = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "main")
        await configure(tmp_path)
        context: dict[str, object] = {
            "branch": "feat/publish",
            "expected_branch_head": target_head,
            "expected_branch": "main",
            "expected_head": controller_head,
        }
        code = (
            "import json,os,sys; from pathlib import Path; "
            "path=Path(sys.argv[1].partition('::')[0]).resolve(); "
            "print(json.dumps({'cwd':os.getcwd(),'file':str(path),'value':path.read_text()}))"
        )
        recipe = JobDefinition(
            "nested",
            "test",
            "task.unit",
            (sys.executable, "-c", code),
            level="unit",
            default_targets=("tests/case.txt",),
            cwd="apps/server",
        )
        selected = replace(task_policy(tmp_path), jobs=(recipe,))
        async with Client(create_server(selected)) as session:
            ready = await success(session, "prepare_project_worktree", context)
            worktree = tmp_path / str(ready["path"])
            started = await success(
                session, START, {**context, "task": "nested", "request_id": "nested-target", "all_tests": True}
            )
            finished_job = await terminal(session, str(started["job_id"]))
            payload = json.loads(str(finished_job["stdout"]))
            assert finished_job["state"] == "succeeded"
            assert finished_job["cwd"] == "apps/server"
            assert finished_job["targets_relative_to"] == "recipe_cwd"
            assert payload == {
                "cwd": str(worktree / "apps/server"),
                "file": str(worktree / "apps/server/tests/case.txt"),
                "value": "target",
            }
            worktree_context = finished_job["worktree"]
            assert isinstance(worktree_context, dict) and worktree_context["verified"] is True
        assert (nested / "case.txt").read_text() == "controller"
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())
