"""M4-P2 failure and cancellation boundaries on disposable linked Worktrees."""

import asyncio
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager, TrailProcessResult
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import success
from tests.support.worktrees import seed

pytestmark = pytest.mark.integration


def _missing_binary(self: IndexManager) -> None:
    return None


def _fake_binary(self: IndexManager) -> Path:
    return Path("/unused/trail")


class _IndexProcessProbe(IndexManager):
    async def run_script(self, script: str) -> TrailProcessResult:
        return await self._run(Path(sys.executable), ("-c", script), timeout=0.5)


async def _fixture(root: Path, factory: WorkspaceFactory) -> tuple[Policy, Path, str, str]:
    await seed(root, factory)
    branch = "feat/m4-recovery"
    target = root / ".lab" / "wt" / "recovery"
    await git(root, "worktree", "add", "-b", branch, str(target), "main")
    return (
        Policy(root, (root,), ("git", "wt", "git.worktree.write", "trail")),
        target,
        branch,
        await git(target, "rev-parse", "HEAD"),
    )


def test_worktree_index_unavailable_timeout_then_successful_retry(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        policy, target, branch, head = await _fixture(tmp_path, workspace_factory)
        args: dict[str, object] = {"branch": branch, "expected_head": head}
        original_head = await git(tmp_path, "rev-parse", "HEAD")
        async with Client(create_server(policy)) as client:
            with monkeypatch.context() as scoped:
                scoped.setattr(IndexManager, "_binary", _missing_binary)
                unavailable = await success(client, "refresh_project_worktree_index", args)
                assert unavailable["outcome"] == "unavailable"
                assert unavailable["structural_state"] == "unavailable"
                assert unavailable["refresh_performed"] is False

            calls: list[tuple[str, ...]] = []
            # First call status: timed-out --check; second refresh: timed-out
            # --check; third refresh: build timeout; fourth: successful retry.
            sequence = [
                TrailProcessResult(None, "private traceback", "timeout"),
                TrailProcessResult(None, "private traceback", "timeout"),
                TrailProcessResult(1, "graph check: STALE\nchanged (1):\n"),
                TrailProcessResult(None, "private traceback", "timeout"),
                TrailProcessResult(1, "graph check: STALE\nchanged (1):\n"),
                TrailProcessResult(0, "build done"),
                TrailProcessResult(0, "graph check: OK\n"),
            ]

            async def fake_run(
                self: IndexManager, binary: Path, argv: tuple[str, ...], timeout: float
            ) -> TrailProcessResult:
                calls.append(argv)
                return sequence.pop(0)

            with monkeypatch.context() as scoped:
                scoped.setattr(IndexManager, "_binary", _fake_binary)
                scoped.setattr(IndexManager, "_run", fake_run)
                before = await success(client, "get_project_worktree_index_status", args)
                assert before["structural_state"] == "unknown"
                assert before["reason"] == "timeout"
                checked = await success(client, "refresh_project_worktree_index", args)
                assert checked["outcome"] == "failed"
                assert checked["reason"] == "timeout"
                assert checked["refresh_performed"] is False
                failed = await success(client, "refresh_project_worktree_index", args)
                assert failed["outcome"] == "failed"
                assert failed["reason"] == "timeout"
                assert failed["refresh_performed"] is True
                recovered = await success(client, "refresh_project_worktree_index", args)
                assert recovered["outcome"] == "updated"
                assert recovered["structural_state"] == "fresh"
                assert recovered["reason"] is None
                assert recovered["ready_for_structural_search"] is True

            assert not sequence
            assert calls == [
                ("build", "--check", str(target)),
                ("build", "--check", str(target)),
                ("build", "--check", str(target)),
                ("build", str(target)),
                ("build", "--check", str(target)),
                ("build", str(target)),
                ("build", "--check", str(target)),
            ]
            assert "private traceback" not in str((before, checked, failed, recovered))
            assert await git(target, "status", "--porcelain") == ""
            assert await git(tmp_path, "status", "--porcelain") == ""
            assert await git(target, "rev-parse", "HEAD") == head
            assert await git(tmp_path, "rev-parse", "HEAD") == original_head

    asyncio.run(scenario())


def test_cancelled_worktree_refresh_releases_root_lock(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        policy, target, branch, head = await _fixture(tmp_path, workspace_factory)
        settings = ServerSettings.load(policy)
        began_build = asyncio.Event()
        cancelled_build = asyncio.Event()
        finished = False

        async def fake_run(
            self: IndexManager, binary: Path, argv: tuple[str, ...], timeout: float
        ) -> TrailProcessResult:
            nonlocal finished
            if "--check" in argv:
                return TrailProcessResult(
                    0 if finished else 1,
                    "graph check: OK\n" if finished else "graph check: STALE\nchanged (1):\n",
                )
            if not began_build.is_set():
                began_build.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled_build.set()
                    raise
            finished = True
            return TrailProcessResult(0, "ok")

        with monkeypatch.context() as scoped:
            scoped.setattr(IndexManager, "_binary", _fake_binary)
            scoped.setattr(IndexManager, "_run", fake_run)
            first = asyncio.create_task(
                settings.worktree_indexes.execute(branch=branch, expected_head=head, action="refresh")
            )
            await asyncio.wait_for(began_build.wait(), timeout=10)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert cancelled_build.is_set()
            second = await asyncio.wait_for(
                settings.worktree_indexes.execute(branch=branch, expected_head=head, action="refresh"),
                timeout=10,
            )
            assert second["outcome"] == "updated"
            assert second["structural_state"] == "fresh"
            third = await asyncio.wait_for(
                settings.worktree_indexes.execute(branch=branch, expected_head=head, action="refresh"),
                timeout=10,
            )
            assert third["outcome"] == "already_fresh"
            assert await git(target, "status", "--porcelain") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("import time; time.sleep(20)", "timeout"),
        ("import sys;sys.stdout.write('X'*140000);sys.stdout.flush();import time;time.sleep(20)", "output_limit"),
    ],
)
def test_index_subprocess_termination_on_timeout_or_output_limit(tmp_path: Path, script: str, expected: str) -> None:
    index = _IndexProcessProbe(Policy(tmp_path, (tmp_path,), ("trail",)), {"PATH": ""})

    async def scenario() -> None:
        report = await index.run_script(script)
        assert report.error == expected
        assert report.exit_code is None
        assert report.output == ""
        await index.close()

    asyncio.run(scenario())
