"""Native and Worktrunk use one safe Git Tree inspection contract."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.tools.git._execution import GitExecution
from local_dev_mcp.tools.git.commands import worktree
from tests.support.git import WorkspaceFactory, git
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


def test_shared_checkout_tree_path_validation(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        policy_config = policy(tmp_path)
        repo = GitExecution(ServerSettings.load(policy_config))
        targets = (tmp_path / ".lab/worktrees/example", tmp_path / ".lab/wt/example")

        for target in targets:
            await worktree.require_safe_checkout_tree(
                repo, policy_config, target, head, operation="Validate disposable checkout"
            )
            restricted = replace(policy_config, denied_paths=(target / "base.txt",))
            with pytest.raises(ToolError, match="access policy"):
                await worktree.require_safe_checkout_tree(
                    repo, restricted, target, head, operation="Validate denied checkout"
                )

        (tmp_path / ".env").write_text("test fixture secret\n")
        await git(tmp_path, "add", "-f", ".env")
        await git(tmp_path, "commit", "-m", "test: tracked protected path")
        protected = await git(tmp_path, "rev-parse", "HEAD")
        for target in targets:
            with pytest.raises(ToolError, match="protected"):
                await worktree.require_safe_checkout_tree(
                    repo, policy_config, target, protected, operation="Reject protected checkout"
                )

    asyncio.run(scenario())
