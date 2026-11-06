"""A newly merged remote main must be observable before its objects exist locally."""

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git._remote import Remote, Result
from local_dev_mcp.tools.git.facades.remote_pr import FetchRemoteMainTool, InspectRemoteTool
from tests.support.git import git, initialize

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("rewrite,alias", [(False, None), (True, None), (False, "before"), (False, "late")])
def test_fetch_unknown_remote_commit_preserves_main_and_ref_on_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewrite: bool, alias: str | None
) -> None:
    async def scenario() -> None:
        upstream, origin, local = (tmp_path / name for name in ("upstream", "origin.git", "local"))
        upstream.mkdir()
        await initialize(upstream)
        await git(upstream, "branch", "-M", "main")
        (upstream / "code.py").write_text("base\n")
        await git(upstream, "add", "code.py")
        await git(upstream, "commit", "-qm", "base")
        base = await git(upstream, "rev-parse", "HEAD")
        await git(tmp_path, "clone", "--bare", "--quiet", str(upstream), str(origin))
        await git(upstream, "remote", "add", "origin", str(origin))
        await git(tmp_path, "clone", "--quiet", str(origin), str(local))
        await initialize(local)
        await git(local, "switch", "-qc", "feat/example")
        if rewrite:
            await git(upstream, "switch", "--orphan", "replacement")
        (upstream / "code.py").write_text("new\n")
        await git(upstream, "add", "code.py")
        await git(upstream, "commit", "-qm", "new remote commit")
        remote_head = await git(upstream, "rev-parse", "HEAD")
        # The force write is confined to this test's private temporary bare repository.
        await git(upstream, "push", "--quiet", "origin", *(("--force",) if rewrite else ()), "HEAD:refs/heads/main")

        async def repo(self: Remote) -> tuple[str, str]:
            return "owner/project", "https://github.com/owner/project"

        async def main_sha(self: Remote, repository: str) -> str:
            assert repository == "owner/project"
            return remote_head

        # Authentication/API identity is the external boundary; object/ref behavior is real Git.
        monkeypatch.setattr(Remote, "repo", repo)
        monkeypatch.setattr(Remote, "main_sha", main_sha)
        settings = ServerSettings.load(Policy(local, (local,), ("git", "gh", "git.remote.read", "git.remote.write")))
        remote = Remote(settings)
        if alias == "before":
            await git(local, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")
        if alias == "late":
            original = Remote.git

            async def late_alias(self: Remote, *args: str, write: bool = False, allow_failure: bool = False) -> Result:
                if args[0] == "update-ref":
                    await git(local, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")
                return await original(self, *args, write=write, allow_failure=allow_failure)

            monkeypatch.setattr(Remote, "git", late_alias)
        assert await remote.local_sha(remote_head) is None
        observed = await InspectRemoteTool(settings).execute()
        assert observed["remote_main"] == remote_head
        assert observed["feature_contains_remote"] is None
        assert not observed["tracking_is_fresh"]
        fetch = FetchRemoteMainTool(settings)
        if rewrite or alias == "before":
            with pytest.raises(ToolError, match="symbolic" if alias else "rewritten or diverged"):
                await fetch.execute("feat/example", base, remote_head)
            assert await remote.local_sha("refs/remotes/origin/main") == base
        else:
            result = await fetch.execute("feat/example", base, remote_head)
            assert result["tracking_main_after"] == remote_head
        assert await remote.local_sha("refs/heads/main") == base
        assert await remote.local_sha("HEAD") == base
        assert await git(local, "status", "--porcelain") == ""

    asyncio.run(scenario())
