"""M4-P3 coordination: independent MCP settings must not run concurrent Trail builds."""

import asyncio
import sys
from pathlib import Path

import pytest

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager, TrailProcessResult
from local_dev_mcp.tools.indexing._process_lock import coordinated_trail_index
from tests.support.git import WorkspaceFactory, git
from tests.support.worktrees import seed

pytestmark = pytest.mark.integration


def _binary(self: IndexManager) -> Path:
    return Path("/test/trail")


def test_two_independent_mcp_instances_serialize_one_worktree(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await seed(tmp_path, workspace_factory)
        branch = "feat/m4-process"
        root = tmp_path / ".lab" / "wt" / "process"
        await git(tmp_path, "worktree", "add", "-b", branch, str(root), "main")
        head = await git(root, "rev-parse", "HEAD")
        policy = Policy(tmp_path, (tmp_path,), ("git", "wt", "git.worktree.write", "trail"))
        first = ServerSettings.load(policy)
        second = ServerSettings.load(policy)
        active = 0
        peak = 0
        built = False

        async def fake_run(self: IndexManager, binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
            nonlocal active, peak, built
            if "--check" in args:
                return TrailProcessResult(
                    0 if built else 1,
                    "graph check: OK\n" if built else "graph check: STALE\nchanged (1):\n",
                )
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.15)
                built = True
            finally:
                active -= 1
            return TrailProcessResult(0, "built")

        monkeypatch.setattr(IndexManager, "_binary", _binary)
        monkeypatch.setattr(IndexManager, "_run", fake_run)
        results = await asyncio.gather(
            first.worktree_indexes.execute(branch=branch, expected_head=head, action="refresh"),
            second.worktree_indexes.execute(branch=branch, expected_head=head, action="refresh"),
        )
        assert peak == 1, "Independent servers must not build one graph concurrently."
        assert sorted(r["outcome"] for r in results) == ["already_fresh", "updated"]
        assert await git(root, "status", "--porcelain") == ""
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


_LOCK_SCRIPT = """
import asyncio
import os
import sys
from pathlib import Path
from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._process_lock import coordinated_trail_index

async def main():
    repo, target, log, action = map(Path, sys.argv[1:5])
    settings = ServerSettings.load(Policy(repo, (repo,), ("git", "trail")))
    async with asyncio.timeout(15):
        async with coordinated_trail_index(settings, target):
            with log.open("a", encoding="utf-8") as f:
                f.write("enter\\n")
            if action.name == "crash":
                os._exit(0)
            await asyncio.sleep(0.4)
            with log.open("a", encoding="utf-8") as f:
                f.write("exit\\n")

asyncio.run(main())
"""


def test_real_os_processes_serialize_and_crash_releases_lock(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await seed(tmp_path, workspace_factory)
        branch = "feat/m4-process"
        root = tmp_path / ".lab" / "wt" / "process"
        await git(tmp_path, "worktree", "add", "-b", branch, str(root), "main")
        head = await git(root, "rev-parse", "HEAD")
        log = tmp_path / ".git" / "m4-process-events.log"

        async def invoke(action: str) -> None:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _LOCK_SCRIPT,
                str(tmp_path),
                str(root),
                str(log),
                action,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, errors = await asyncio.wait_for(process.communicate(), timeout=25)
            assert process.returncode == 0, (output.decode(errors="replace"), errors.decode(errors="replace"))

        await asyncio.gather(invoke("refresh"), invoke("refresh"))
        assert log.read_text(encoding="utf-8").splitlines() == ["enter", "exit", "enter", "exit"]
        await invoke("crash")
        assert log.read_text(encoding="utf-8").splitlines()[-1] == "enter"
        # The crashed owner cannot leave a stale advisory OS lock.
        await invoke("refresh")
        assert log.read_text(encoding="utf-8").splitlines()[-2:] == ["enter", "exit"]
        assert await git(root, "rev-parse", "HEAD") == head
        assert await git(root, "status", "--porcelain") == ""
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_contended_lock_timeout_preserves_files_and_allows_retry(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await seed(tmp_path, workspace_factory)
        root = tmp_path / ".lab" / "wt" / "process"
        await git(tmp_path, "worktree", "add", "-b", "feat/m4-process", str(root), "main")
        settings = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git", "trail")))
        async with coordinated_trail_index(settings, root):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    async with coordinated_trail_index(settings, root):
                        pytest.fail("Should not acquire a held interprocess lock.")

            async def waiting() -> None:
                async with coordinated_trail_index(settings, root):
                    pytest.fail("Cancelled waiter must never acquire the lock.")

            task = asyncio.create_task(waiting())
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with coordinated_trail_index(settings, root):
            pass
        assert await git(root, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_different_roots_are_not_serialized_by_one_process_lock(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await seed(tmp_path, workspace_factory)
        alpha = tmp_path / ".lab" / "wt" / "alpha"
        beta = tmp_path / ".lab" / "wt" / "beta"
        for branch, root in (("feat/m4-alpha", alpha), ("feat/m4-beta", beta)):
            await git(tmp_path, "worktree", "add", "-b", branch, str(root), "main")
        settings = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git", "trail")))
        reached = asyncio.Event()
        active = 0
        peak = 0

        async def use(root: Path) -> None:
            nonlocal active, peak
            async with coordinated_trail_index(settings, root):
                active += 1
                peak = max(peak, active)
                reached.set()
                await asyncio.sleep(0.1)
                active -= 1

        await asyncio.gather(use(alpha), use(beta))
        assert reached.is_set()
        assert peak == 2
        assert await git(alpha, "status", "--porcelain") == ""
        assert await git(beta, "status", "--porcelain") == ""

    asyncio.run(scenario())
