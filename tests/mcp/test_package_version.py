"""MCP identifies the installed product rather than a duplicated version literal."""

import asyncio
from importlib import metadata
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory

pytestmark = pytest.mark.integration


def test_mcp_uses_installed_distribution_version(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = metadata.version

    def candidate_version(name: str) -> str:
        return "7.6.5" if name == "local-dev-mcp" else installed(name)

    monkeypatch.setattr(metadata, "version", candidate_version)

    async def scenario() -> None:
        await workspace_factory()
        server = create_server(Policy(tmp_path, (tmp_path,), ("git",)))
        async with Client(server):
            assert server.version == "7.6.5"

    asyncio.run(scenario())
