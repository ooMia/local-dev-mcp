"""Verify public package imports in fresh interpreters, without cached import order."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from shellous import sh

from local_dev_mcp.execution import base_environment


@pytest.mark.parametrize("first", ["local_dev_mcp.runtime", "local_dev_mcp.tools.git"])
def test_public_imports_preserve_registration_identity(tmp_path: Path, first: str) -> None:
    script = f"""
import importlib
importlib.import_module({first!r})
from local_dev_mcp.core import TOOLS, create_server
from local_dev_mcp.core import server
from local_dev_mcp.tools import registry
from local_dev_mcp.tools.git import GET_PROJECT_HEAD, PreparedGitCommit
from local_dev_mcp.runtime import ServerSettings
assert TOOLS is server.TOOLS
assert create_server is server.create_server
assert TOOLS[0] is GET_PROJECT_HEAD
assert all(isinstance(tool, registry.ToolDefinition) for tool in TOOLS)
assert PreparedGitCommit in ServerSettings.__annotations__["commit"].__args__
"""
    run_import(tmp_path, script, isolate_fastmcp=True)


@pytest.mark.parametrize(
    "module",
    ["local_dev_mcp.tools", "local_dev_mcp.tools.gpg._wrapper", "local_dev_mcp.tools.tunnel_client"],
)
def test_support_imports_do_not_load_fastmcp_or_dotenv(tmp_path: Path, module: str) -> None:
    # FastMCP would read this invalid setting if imported before CLI isolation.
    (tmp_path / ".env").write_text("FASTMCP_LOG_LEVEL=invalid-import-canary\nIMPORT_CANARY=must-not-load\n")
    script = f"""
import importlib
import os
import sys
importlib.import_module({module!r})
assert "fastmcp" not in sys.modules
assert "local_dev_mcp.tools.registry" not in sys.modules
assert "local_dev_mcp.tools.gpg.configuration" not in sys.modules
assert "IMPORT_CANARY" not in os.environ
"""
    run_import(tmp_path, script, isolate_fastmcp=False)


def run_import(root: Path, script: str, *, isolate_fastmcp: bool) -> None:
    environment = base_environment()
    if isolate_fastmcp:
        environment["FASTMCP_ENV_FILE"] = os.devnull

    async def scenario() -> None:
        await sh(sys.executable, "-I", "-c", script).set(cwd=root, inherit_env=False, env=environment, timeout=15)

    asyncio.run(scenario())
