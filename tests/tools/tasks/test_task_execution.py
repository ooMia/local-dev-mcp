"""Exercise configured task processes, bounded diagnostics and response-loss recovery."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import local_dev_mcp.tools.tasks._manager as task_runtime
from local_dev_mcp.redaction import WITHHELD, Redactor
from local_dev_mcp.settings import JobDefinition, JobKind, Policy
from local_dev_mcp.tools.tasks import TaskManager, TaskWorktree
from local_dev_mcp.tools.tasks._response import TaskResult

pytestmark = pytest.mark.integration


def test_navigation_ancestor_cannot_broaden_a_scoped_test_selection(tmp_path: Path) -> None:
    allowed = tmp_path / "tests" / "allowed"
    allowed.mkdir(parents=True)
    policy = Policy(tmp_path, (allowed,), ("task.unit",))
    recipe = JobDefinition(
        "unit",
        "test",
        "task.unit",
        (sys.executable, "-c", "import sys; print(sys.argv[1])"),
        level="unit",
        default_targets=("tests/allowed",),
    )
    tasks = TaskManager(policy, (recipe,), {"PATH": os.defpath}, Redactor.from_values({}))

    async def scenario() -> None:
        try:
            with pytest.raises(ToolError, match="permitted workspace scope"):
                await tasks.start("unit", "broad-ancestor", ["tests"])
            result = await tasks.start("unit", "allowed-subtree", ["tests/allowed"])
            assert result["state"] == "succeeded"
            assert tasks.get(result["job_id"], include_output=True)["stdout"] == "tests/allowed\n"
        finally:
            await tasks.close()

    asyncio.run(scenario())


def manager(
    root: Path,
    code: str,
    *,
    kind: JobKind = "lint",
    timeout: int = 300,
    secret: str = "synthetic-private-task-value",
) -> TaskManager:
    policy = Policy(root, (root,), ("task.check",))
    recipe = JobDefinition(
        "check",
        kind,
        "task.check",
        (sys.executable, "-c", code),
        level="unit" if kind == "test" else None,
        default_targets=("tests",) if kind == "test" else (),
        timeout_seconds=timeout,
    )
    return TaskManager(
        policy,
        (recipe,),
        {"PATH": os.defpath, "TASK_SECRET": secret},
        Redactor.from_values({"TASK_SECRET": secret}),
    )


async def finished(tasks: TaskManager, job_id: str, *, include_output: bool = False) -> TaskResult:
    async with asyncio.timeout(5):
        while True:
            result = tasks.get(job_id)
            if result["state"] != "running":
                return tasks.get(job_id, include_output=True) if include_output else result
            await asyncio.sleep(0.01)


async def ready(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


def test_metadata_polls_do_not_repeat_output_and_details_never_reexecute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 0.02)
    code = (
        "import os,time; from pathlib import Path; p=Path('calls'); "
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
        "os.write(1,('x'*16000+os.environ['TASK_SECRET']+'\\n').encode()); "
        "os.write(2,(os.environ['TASK_SECRET']+'\\n').encode()); Path('ready').touch(); "
        "exec(\"while not Path('release').exists(): time.sleep(.01)\")"
    )
    tasks = manager(tmp_path, code)

    async def scenario() -> None:
        try:
            started = await tasks.start("check", "retained-output")
            assert started["state"] == "running"
            assert started["output_included"] is False and started["stdout"] == started["stderr"] == ""
            job_id = started["job_id"]
            await ready(tmp_path / "ready")
            async with asyncio.timeout(5):
                while True:
                    partial = tasks.get(job_id, include_output=True)
                    if len(partial["stdout"]) > 1000:
                        break
                    await asyncio.sleep(0.01)
            assert partial["state"] == "running" and partial["output_included"] is True
            for _ in range(5):
                metadata = tasks.get(job_id)
                assert metadata["stdout"] == metadata["stderr"] == "" and metadata["output_included"] is False
                assert metadata["output_complete"] is False and metadata["output_reason"] is None
                assert len(json.dumps(metadata).encode()) < 4096
            (tmp_path / "release").touch()
            terminal = await finished(tasks, job_id)
            assert terminal["state"] == "succeeded" and terminal["output_included"] is False
            details = tasks.get(job_id, include_output=True)
            assert details["stdout"] == "x" * 16000 + "[REDACTED]\n"
            assert details["stderr"] == "[REDACTED]\n" and details["output_complete"] is True
            repeated = await tasks.start("check", "retained-output")
            cancelled = await tasks.cancel(job_id)
            assert repeated["job_id"] == cancelled["job_id"] == job_id
            assert repeated["output_included"] is cancelled["output_included"] is False
            assert repeated["stdout"] == cancelled["stdout"] == ""
            assert (tmp_path / "calls").read_text() == "1"
        finally:
            await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("secret", "code", "expected"),
    [("synthetic-private-task-value", "pass", ""), ("x", "print('private diagnostic')", WITHHELD)],
)
def test_omitted_empty_and_withheld_output_have_distinct_meaning(
    tmp_path: Path, secret: str, code: str, expected: str
) -> None:
    tasks = manager(tmp_path, code, secret=secret)

    async def scenario() -> None:
        try:
            metadata = await tasks.start("check", "output-meaning")
            assert metadata["state"] == "succeeded" and metadata["output_included"] is False
            assert metadata["stdout"] == metadata["stderr"] == ""
            details = tasks.get(metadata["job_id"], include_output=True)
            assert details["output_included"] is True
            assert details["stdout"] == details["stderr"] == expected
            assert details["output_complete"] == metadata["output_complete"] == (not expected)
            assert details["output_reason"] == metadata["output_reason"]
            assert (details["output_reason"] is None) == (not expected)
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_recipe_uses_target_workspace_snapshot_environment_and_exact_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOULD_NOT_REACH_TASK", "ambient-private-canary")
    policy = Policy(tmp_path, (tmp_path,), ("uv", "task.check"))
    code = (
        "import json,os,sys; print(json.dumps({'cwd':os.getcwd(),"
        "'ambient':os.environ.get('SHOULD_NOT_REACH_TASK'),'stdin':sys.stdin.read(),'prefix':sys.prefix}))"
    )
    recipes = (
        JobDefinition("allowed", "lint", "task.check", (sys.executable, "-c", code)),
        JobDefinition("denied", "lint", "uv.run.pytest", (sys.executable, "-c", "raise AssertionError")),
        JobDefinition("missing", "lint", "task.check", ("nonexistent-task-command-2345",)),
    )
    tasks = TaskManager(policy, recipes, {"PATH": os.defpath}, Redactor.from_values({}))
    catalogue = {item["name"]: item for item in tasks.catalogue()}
    assert catalogue["allowed"]["ready"] is True
    assert catalogue["denied"]["reason"] == "permission_denied"
    assert catalogue["missing"]["reason"] == "executable_unavailable"

    async def scenario() -> None:
        try:
            result = await tasks.start("allowed", "allowed-1")
            assert result["state"] == "succeeded" and result["termination_confirmed"]
            assert json.loads(tasks.get(result["job_id"], include_output=True)["stdout"]) == {
                "cwd": str(tmp_path),
                "ambient": None,
                "stdin": "",
                "prefix": sys.prefix,
            }
            with pytest.raises(ToolError, match="not ready"):
                await tasks.start("denied", "denied-1")
            with pytest.raises(ToolError, match="not ready"):
                await tasks.start("missing", "missing-1")
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_explicit_test_selection_replaces_full_targets_and_keeps_node_parameters(tmp_path: Path) -> None:
    tasks = manager(tmp_path, "import json,sys; print(json.dumps(sys.argv[1:]))", kind="test")
    selected = "tests/new_case.py::test_value[param/../unchanged]"

    async def scenario() -> None:
        try:
            result = await tasks.start("check", "targeted", [selected])
            assert json.loads(tasks.get(result["job_id"], include_output=True)["stdout"]) == [selected]
            assert result["targets"] == [selected] and result["all_tests"] is False
            entire = await tasks.start("check", "full", all_tests=True)
            assert json.loads(tasks.get(entire["job_id"], include_output=True)["stdout"]) == ["tests"]
            for targets in ([], ["-q"], ["@arguments"], ["../test.py"], ["/tmp/test.py"], ["x\0y"]):
                with pytest.raises(ToolError):
                    await tasks.start("check", "invalid", targets)
            with pytest.raises(ToolError, match="Select"):
                await tasks.start("check", "missing-selection")
            with pytest.raises(ToolError, match="never both"):
                await tasks.start("check", "ambiguous", [selected], all_tests=True)
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_both_output_streams_are_drained_redacted_and_bounded(tmp_path: Path) -> None:
    tasks = manager(
        tmp_path,
        "import os; data=(os.environ['TASK_SECRET']+'x').encode()*12000; os.write(1,data); os.write(2,data)",
    )

    async def scenario() -> None:
        try:
            initial = await tasks.start("check", "large-output")
            result = await finished(tasks, initial["job_id"], include_output=True)
            assert result["state"] == "succeeded" and result["exit_code"] == 0
            for text in (result["stdout"], result["stderr"]):
                assert "synthetic-private-task-value" not in text
                assert "[REDACTED]" in text
                assert len(text.encode()) <= 65536
            assert result["output_complete"] is False and result["output_reason"] is not None
            metadata = tasks.get(initial["job_id"])
            assert metadata["stdout"] == metadata["stderr"] == "" and metadata["output_included"] is False
            assert metadata["output_complete"] == result["output_complete"]
            assert metadata["output_reason"] == result["output_reason"]
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_duplicate_start_after_response_loss_keeps_one_process_and_one_active_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 10.0)
    tasks = manager(
        tmp_path,
        "import pathlib,time; pathlib.Path('ready').write_text('ready'); time.sleep(.2); "
        "pathlib.Path('calls').write_text('once')",
    )

    async def scenario() -> None:
        try:
            start = asyncio.create_task(tasks.start("check", "lost-response"))
            await ready(tmp_path / "ready")
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
            recovered = await tasks.start("check", "lost-response")
            assert recovered["state"] == "running"
            with pytest.raises(ToolError, match="active"):
                await tasks.start("check", "second-job")
            result = await finished(tasks, recovered["job_id"])
            assert result["state"] == "succeeded"
            duplicate = await tasks.start("check", "lost-response")
            assert duplicate["job_id"] == result["job_id"]
            assert (tmp_path / "calls").read_text() == "once"
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_cancel_and_timeout_confirm_direct_child_exit_without_claiming_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 0.02)
    code = "import pathlib,time; pathlib.Path('ready').write_text('changed'); time.sleep(30)"
    tasks = manager(tmp_path, code)

    async def scenario() -> None:
        try:
            started = await tasks.start("check", "cancel-me")
            await ready(tmp_path / "ready")
            cancelled = await tasks.cancel(started["job_id"])
            assert cancelled["state"] == "cancelled"
            assert cancelled["termination_confirmed"] is True and cancelled["exit_code"] is not None
            assert cancelled["may_have_changed_files"] is True
            assert (tmp_path / "ready").read_text() == "changed"
            assert (await tasks.cancel(started["job_id"]))["state"] == "cancelled"
        finally:
            await tasks.close()
        timed = manager(tmp_path, code, timeout=1)
        try:
            started = await timed.start("check", "timeout")
            result = await finished(timed, started["job_id"])
            assert result["state"] == "timed_out" and result["termination_confirmed"] is True
            assert result["may_have_changed_files"] is True
        finally:
            await timed.close()

    asyncio.run(scenario())


def test_result_eviction_keeps_request_tombstone_and_ledger_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_runtime, "MAX_RESULTS", 1)
    monkeypatch.setattr(task_runtime, "MAX_REQUESTS", 2)
    tasks = manager(
        tmp_path,
        "from pathlib import Path; p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    )

    async def scenario() -> None:
        try:
            first = await tasks.start("check", "first")
            second = await tasks.start("check", "second")
            assert first["state"] == second["state"] == "succeeded"
            with pytest.raises(ToolError, match="expired"):
                tasks.get(first["job_id"])
            with pytest.raises(ToolError, match="expired"):
                await tasks.start("check", "first")
            with pytest.raises(ToolError, match="ledger is full"):
                await tasks.start("check", "third")
            assert (tmp_path / "count").read_text() == "2"
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_request_conflict_and_server_restart_never_reuse_an_old_job(tmp_path: Path) -> None:
    tasks = manager(tmp_path, "print('ok')", kind="test")

    async def scenario() -> None:
        try:
            result = await tasks.start("check", "same-request", ["tests/first.py"])
            with pytest.raises(ToolError, match="different task arguments"):
                await tasks.start("check", "same-request", ["tests/second.py"])
            fresh = manager(tmp_path, "print('ok')", kind="test")
            try:
                with pytest.raises(ToolError, match="unknown or expired"):
                    fresh.get(result["job_id"])
            finally:
                await fresh.close()
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_close_cancels_active_process_and_prevents_new_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 0.02)
    tasks = manager(tmp_path, "import pathlib,time; pathlib.Path('ready').touch(); time.sleep(30)")

    async def scenario() -> None:
        started = await tasks.start("check", "shutdown")
        await ready(tmp_path / "ready")
        await tasks.close()
        assert tasks.get(started["job_id"])["state"] == "cancelled"
        assert tasks.get(started["job_id"])["termination_confirmed"] is True
        with pytest.raises(ToolError, match="closing"):
            await tasks.start("check", "after-close")

    asyncio.run(scenario())


def test_project_relative_executable_is_fixed_and_later_removal_is_start_failure(tmp_path: Path) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    executable = folder / "check"
    executable.write_text(f"#!{sys.executable}\nprint('project executable')\n")
    executable.chmod(0o755)
    policy = Policy(tmp_path, (tmp_path,), ("task.check",))
    recipe = JobDefinition("check", "lint", "task.check", ("bin/check",))
    tasks = TaskManager(policy, (recipe,), {"PATH": os.defpath}, Redactor.from_values({}))

    async def scenario() -> None:
        try:
            result = await tasks.start("check", "relative-binary")
            assert result["state"] == "succeeded"
            assert tasks.get(result["job_id"], include_output=True)["stdout"] == "project executable\n"
            executable.unlink()
            unavailable = await tasks.start("check", "removed-binary")
            assert unavailable["state"] == "start_failed" and unavailable["termination_confirmed"]
            assert unavailable["may_have_changed_files"] is False
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_retry_recovers_job_when_target_later_becomes_an_outside_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 0.02)
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    tests = workspace / "tests"
    tests.mkdir()
    tasks = manager(workspace, "import pathlib,time; pathlib.Path('ready').touch(); time.sleep(30)", kind="test")

    async def scenario() -> None:
        try:
            started = await tasks.start("check", "target-changed", ["tests"])
            await ready(workspace / "ready")
            tests.rmdir()
            tests.symlink_to(outside, target_is_directory=True)
            recovered = await tasks.start("check", "target-changed", ["./tests"])
            assert recovered["job_id"] == started["job_id"]
            await tasks.cancel(started["job_id"])
            with pytest.raises(ToolError, match="outside"):
                await tasks.start("check", "new-run", ["tests"])
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_unconfirmed_cleanup_keeps_workspace_busy_and_exposes_recovery_job_id(tmp_path: Path) -> None:
    tasks = manager(tmp_path, "print('complete')")

    async def scenario() -> None:
        try:
            result = await tasks.start("check", "uncertain")
            # Inject a termination observation failure without leaking a child.
            record = tasks._results[result["job_id"]]  # pyright: ignore[reportPrivateUsage]
            record.state = "cleanup_unconfirmed"
            record.termination_confirmed = False
            tasks._active = result["job_id"]  # pyright: ignore[reportPrivateUsage]
            tasks._finish(record)  # pyright: ignore[reportPrivateUsage]
            with pytest.raises(ToolError, match=f"active_job_id={result['job_id']}"):
                await tasks.start("check", "unsafe-new-run")
            assert tasks.get(result["job_id"])["state"] == "cleanup_unconfirmed"
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_normalization_keeps_option_and_argument_file_names_as_literal_paths(tmp_path: Path) -> None:
    (tmp_path / "@arguments").write_text("--unsafe-option\n")
    code = (
        "import argparse,json; p=argparse.ArgumentParser(fromfile_prefix_chars='@'); "
        "p.add_argument('targets',nargs='+'); print(json.dumps(p.parse_args().targets))"
    )
    tasks = manager(tmp_path, code, kind="test")

    async def scenario() -> None:
        try:
            result = await tasks.start("check", "literal-files", [".//-q", ".//@arguments"])
            assert result["state"] == "succeeded"
            assert json.loads(tasks.get(result["job_id"], include_output=True)["stdout"]) == ["./-q", "./@arguments"]
            repeated = await tasks.start("check", "literal-files", ["./-q", "./@arguments"])
            assert repeated["job_id"] == result["job_id"]
        finally:
            await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("environment", [{"INVALID": "contains\0nul"}, {"BAD=NAME": "value"}])
def test_invalid_environment_is_unready_without_locking_workspace(tmp_path: Path, environment: dict[str, str]) -> None:
    policy = Policy(tmp_path, (tmp_path,), ("task.check",))
    recipe = JobDefinition("check", "lint", "task.check", (sys.executable, "-c", "print('unreached')"))
    tasks = TaskManager(policy, (recipe,), environment, Redactor.from_values({}))
    assert tasks.catalogue()[0]["reason"] == "environment_invalid"

    async def scenario() -> None:
        try:
            with pytest.raises(ToolError, match="environment_invalid"):
                await tasks.start("check", "invalid-env-one")
            with pytest.raises(ToolError, match="environment_invalid"):
                await tasks.start("check", "invalid-env-two")
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_worktree_job_remains_running_until_post_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 0.02)
    tasks = manager(tmp_path, "print('done')")

    async def scenario() -> None:
        selected = tmp_path / "target"
        selected.mkdir()
        scoped = Policy(selected, (selected,), ("task.check",))
        verifying, release = asyncio.Event(), asyncio.Event()

        async def prepare() -> Policy:
            return scoped

        async def verify() -> None:
            verifying.set()
            await release.wait()

        context = TaskWorktree("feat/check", "a" * 40, "main", "b" * 40, prepare, verify)
        try:
            job = await tasks.start("check", "post-verify", worktree=context)
            await asyncio.wait_for(verifying.wait(), timeout=5)
            observed = tasks.get(job["job_id"])
            assert observed["state"] == "running"
            assert observed.get("worktree") is not None
            with pytest.raises(ToolError, match="active"):
                await tasks.start("check", "other")
            release.set()
            result = await finished(tasks, job["job_id"])
            assert result["state"] == "succeeded"
            details = result.get("worktree")
            assert details is not None and details["verified"] is True
        finally:
            release.set()
            await tasks.close()

    asyncio.run(scenario())


def test_cancel_during_worktree_post_verification_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 0.02)
    tasks = manager(tmp_path, "print('done')")

    async def scenario() -> None:
        selected = tmp_path / "target"
        selected.mkdir()
        verifying, release = asyncio.Event(), asyncio.Event()

        async def prepare() -> Policy:
            return Policy(selected, (selected,), ("task.check",))

        async def verify() -> None:
            verifying.set()
            await release.wait()

        context = TaskWorktree("feat/check", "a" * 40, "main", "b" * 40, prepare, verify)
        try:
            job = await tasks.start("check", "cancel-post", worktree=context)
            await asyncio.wait_for(verifying.wait(), timeout=5)
            result = await tasks.cancel(job["job_id"])
            assert result["state"] == "cancelled" and result["termination_confirmed"] is True
            details = result.get("worktree")
            assert details is not None and details["verified"] is False
        finally:
            release.set()
            await tasks.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("close_during_prepare", [False, True])
def test_async_worktree_preparation_retains_single_slot_and_honors_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_during_prepare: bool
) -> None:
    monkeypatch.setattr(task_runtime, "SHORT_WAIT_SECONDS", 0.02)
    tasks = manager(tmp_path, "import time; time.sleep(20)")

    async def scenario() -> None:
        selected = tmp_path / "target"
        selected.mkdir()
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare() -> Policy:
            preparing.set()
            await release.wait()
            return Policy(selected, (selected,), ("task.check",))

        async def verify() -> None:
            pass

        context = TaskWorktree("feat/check", "a" * 40, "main", "b" * 40, prepare, verify)
        submission = asyncio.create_task(tasks.start("check", "preflight", worktree=context))
        try:
            await asyncio.wait_for(preparing.wait(), timeout=5)
            if close_during_prepare:
                await tasks.close()
                release.set()
                with pytest.raises(ToolError, match="closed"):
                    await submission
            else:
                other = asyncio.create_task(tasks.start("check", "concurrent"))
                await asyncio.sleep(0.02)
                assert not other.done()
                release.set()
                job = await submission
                with pytest.raises(ToolError, match="active"):
                    await other
                await tasks.cancel(job["job_id"])
        finally:
            release.set()
            await tasks.close()
            if not submission.done():
                await asyncio.gather(submission, return_exceptions=True)

    asyncio.run(scenario())
