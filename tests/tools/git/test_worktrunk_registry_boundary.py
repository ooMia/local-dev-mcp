"""The Worktrunk MCP surface produces a reviewed proposal, never local main integration."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter
from tests.support.workspace import configure
from tests.support.worktrees import policy

pytestmark = pytest.mark.integration


def test_worktrunk_has_no_executable_merge_tool(tmp_path: Path) -> None:
    configure(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(policy(tmp_path))) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "create_project_worktrunk" in names
            assert "verify_project_worktrunk_quality" in names
            assert "get_project_worktrunk_merge_plan" in names
            assert "remove_project_worktrunk" in names
            assert "merge_project_worktrunk" not in names
            assert "prepare_project_worktrunk_main" not in names
            # Existing, independent GitHub PR workflow remains registered.
            assert "get_project_pr_merge_plan" in names
            assert "merge_project_pr" in names

    asyncio.run(scenario())


def test_worktrunk_adapter_does_not_offer_internal_main_merge() -> None:
    assert not hasattr(WorktrunkAdapter, "merge")
