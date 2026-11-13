"""Release PR publication validates real committed release snapshots, not synthetic assessments."""

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.execution import base_environment
from local_dev_mcp.tools.git import _remote
from local_dev_mcp.tools.git.facades.remote_pr import _release_facts  # pyright: ignore[reportPrivateUsage]  # This integration test exercises the private publication validator.
from local_dev_mcp.tools.release import prepare_release, read_snapshot
from local_dev_mcp.tools.release._git import LocalGit
from tests.support.git import WorkspaceFactory, git
from tests.support.release import release_workspace

pytestmark = pytest.mark.integration


@dataclass
class LocalRemote:
    root: Path

    async def git(self, *args: str) -> _remote.Result:
        content = await LocalGit(self.root).read(*args)
        return _remote.Result(0, content.decode("utf-8"))


def test_release_pr_checks_receipt_from_real_git_snapshot(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await release_workspace(tmp_path, workspace_factory, pending=True)
        subprocess.run(
            ["uv", "lock", "--offline", "--no-config", "--cache-dir", str(tmp_path.parent / "uv-cache")],
            cwd=tmp_path,
            env=base_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
        await git(tmp_path, "add", "uv.lock")
        await git(tmp_path, "commit", "-m", "build: lock initial project")
        base = await git(tmp_path, "rev-parse", "HEAD")
        digest = (await read_snapshot(LocalGit(tmp_path).read, base)).plan()["digest"]
        await git(tmp_path, "switch", "-c", "release/1.3.0")
        await prepare_release(tmp_path, base, digest)
        await git(tmp_path, "add", "-A")
        await git(tmp_path, "commit", "-m", "chore(release): prepare v1.3.0")
        head = await git(tmp_path, "rev-parse", "HEAD")

        remote = cast(_remote.Remote, LocalRemote(tmp_path))
        result = await _release_facts(remote, base, head, "release/1.3.0")
        assert result["version"] == "1.3.0"
        assert result["source_head"] == base
        assert result["consumed"] == ["previous"]
        assert result["plan_digest"] == digest
        with pytest.raises(ToolError, match="branch version"):
            await _release_facts(remote, base, head, "release/1.3.1")

        (tmp_path / "unrelated.txt").write_text("not a release output")
        await git(tmp_path, "add", "unrelated.txt")
        await git(tmp_path, "commit", "-m", "chore: introduce unreviewed content")
        invalid_head = await git(tmp_path, "rev-parse", "HEAD")
        with pytest.raises(ToolError, match="Invalid release snapshot"):
            await _release_facts(remote, base, invalid_head, "release/1.3.0")

    asyncio.run(scenario())
