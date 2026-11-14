"""A clean registered Worktrunk can update origin/main while its controller is dirty."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.tools.git import _remote
from local_dev_mcp.tools.git.facades import remote_pr
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


def test_fetch_remote_main_via_worktrunk_preserves_dirty_controller(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        origin = tmp_path.parent / (tmp_path.name + "-fetch-main.git")
        await git(tmp_path, "init", "--bare", str(origin))
        await git(tmp_path, "remote", "add", "origin", str(origin))
        await git(tmp_path, "push", "origin", "main")
        await git(tmp_path, "update-ref", "refs/remotes/origin/main", base)
        nested = tmp_path / ".lab" / "fetch-main"
        await git(tmp_path, "worktree", "add", str(nested), "feat/publish")
        (tmp_path / "new-main.txt").write_text("new upstream snapshot\n")
        await git(tmp_path, "add", "new-main.txt")
        await git(tmp_path, "commit", "-m", "feat: update main")
        new_main = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "push", "origin", "main")
        await git(tmp_path, "update-ref", "refs/remotes/origin/main", base)
        (tmp_path / "base.txt").write_text("dirty controller user value\n")

        class LocalRemote(_remote.Remote):
            async def repo(self) -> tuple[str, str]:
                return "example/project", "https://github.com/example/project"

            async def main_sha(self, name: str) -> str:
                response = await git(self.root, "ls-remote", "--heads", "origin", "refs/heads/main")
                return response.split("\t")[0]

        monkeypatch.setattr(remote_pr, "Remote", LocalRemote)
        permissions = replace(
            policy(tmp_path),
            allowed_commands=("git", "wt", "git.worktree.write", "gh", "git.remote.write"),
        )
        async with Client(create_server(permissions)) as client:
            registry = {entry.name: entry for entry in await client.list_tools()}
            tool = registry["fetch_project_branch_remote_main"]
            assert tool.input_schema["additionalProperties"] is False
            assert "path" not in tool.input_schema["properties"]
            assert "allow_dirty_controller" not in tool.input_schema["properties"]
            await failure(
                client,
                "fetch_project_remote_main",
                {
                    "expected_branch": "main",
                    "expected_head": new_main,
                    "expected_remote_main": new_main,
                },
            )
            await failure(
                client,
                "fetch_project_branch_remote_main",
                {
                    "branch": "feat/publish",
                    "expected_branch_head": "f" * 40,
                    "expected_remote_main": new_main,
                },
            )
            result = await success(
                client,
                "fetch_project_branch_remote_main",
                {
                    "branch": "feat/publish",
                    "expected_branch_head": base,
                    "expected_remote_main": new_main,
                },
            )
            assert result["changed"] is True
            assert result["tracking_main_before"] == base
            assert result["tracking_main_after"] == new_main
            assert result["remote_main"] == new_main
            assert await git(tmp_path, "rev-parse", "origin/main") == new_main
            assert await git(nested, "rev-parse", "HEAD") == base
            assert await git(nested, "status", "--porcelain") == ""
            assert await git(tmp_path, "rev-parse", "HEAD") == new_main
            assert (tmp_path / "base.txt").read_text() == "dirty controller user value\n"
            again = await success(
                client,
                "fetch_project_branch_remote_main",
                {
                    "branch": "feat/publish",
                    "expected_branch_head": base,
                    "expected_remote_main": new_main,
                },
            )
            assert again["changed"] is False

    asyncio.run(scenario())
