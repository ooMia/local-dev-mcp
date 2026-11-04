"""Validate installed entry points rather than importing source as evidence."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from shellous import sh

from local_dev_mcp.execution import base_environment
from tests.support.git import WorkspaceFactory, git
from tests.support.packaging import InstalledPackage
from tests.support.signing import SigningSetup

pytestmark = pytest.mark.packaging


def test_installed_cli_stdio_signed_commit(
    installed_package: InstalledPackage,
    workspace_factory: WorkspaceFactory,
    signing_setup: SigningSetup,
) -> None:
    async def scenario() -> None:
        root = await workspace_factory(signing=signing_setup)
        (root / "config.toml").write_text(
            "[filesystem]\nallowed_paths=" + json.dumps([str(root)]) + '\n[system]\nallowed_commands=["git", "gpg"]\n'
        )
        # -I and a fresh workspace keep source-tree imports out of this probe.
        module = await sh(
            installed_package.python,
            "-I",
            "-c",
            "from pathlib import Path; import local_dev_mcp; print(Path(local_dev_mcp.__file__).resolve())",
        ).set(cwd=root, inherit_env=False, env=base_environment(), timeout=10)
        assert Path(module.strip()).is_relative_to(installed_package.python.parent.parent.resolve())
        assert installed_package.cli.is_file()
        assert installed_package.wrapper.is_file()
        preflight = await sh.result(
            installed_package.python, "-I", "-m", "local_dev_mcp.tools.tunnel_client._preflight"
        ).set(cwd=root, inherit_env=False, env={**base_environment(), "FASTMCP_ENV_FILE": os.devnull}, timeout=10)
        assert preflight.exit_code == 0
        assert json.loads(preflight.output) == {"warnings": []}
        (root / "data").write_text("installed signed commit\n")
        async with Client(
            StdioServerParameters(command=str(installed_package.cli), args=["run"], cwd=root, env=base_environment()),
            read_timeout_seconds=30,
            cache=None,
        ) as session:
            tools = await session.list_tools()
            assert "commit_project_changes" in [tool.name for tool in tools.tools]
            staged = await session.call_tool("stage_project_paths", {"paths": ["data"]})
            assert not staged.is_error, staged.content
            committed = await session.call_tool("commit_project_changes", {"message": "Installed package signing"})
            assert not committed.is_error, committed.content
            result = committed.structured_content
            assert result is not None
            assert result["success"] is True
            assert result["signing_requested"] is True
            assert result["signature_verified"] is True
            head = await session.call_tool("get_project_head")
            assert not head.is_error
            assert head.structured_content is not None
            assert head.structured_content["head"] == await git(root, "rev-parse", "HEAD")
        assert "gpgsig " in await git(root, "cat-file", "commit", "HEAD")

    asyncio.run(scenario())
