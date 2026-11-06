"""New GitHub publication MCP tools are discoverable but permission-gated."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory

pytestmark = pytest.mark.integration

PUBLICATION_TOOLS = {
    "diagnose_project_remote_auth",
    "inspect_project_remote",
    "fetch_project_remote_main",
    "push_project_feature",
    "create_project_draft_pr",
}


def test_publication_tool_schema_and_scoped_permissions(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        server = create_server(Policy(tmp_path, (tmp_path,), ("git",)))
        async with Client(server) as session:
            declared = {tool.name: tool for tool in await session.list_tools()}
            assert PUBLICATION_TOOLS <= declared.keys()
            push = declared["push_project_feature"]
            assert {"expected_branch", "expected_head", "expected_remote_main"} <= set(push.input_schema["required"])
            assert push.input_schema["additionalProperties"] is False
            create = declared["create_project_draft_pr"]
            assert {"expected_branch", "expected_head", "expected_remote_main", "title_text"} <= set(
                create.input_schema["required"]
            )
            for name in PUBLICATION_TOOLS:
                result = await session.call_tool(name, raise_on_error=False)
                assert result.is_error

    asyncio.run(scenario())
