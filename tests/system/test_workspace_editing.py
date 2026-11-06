"""Recover a real CLI stdio edit after the user inserts lines in the target workspace."""

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from local_dev_mcp.execution import base_environment
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def test_cli_observed_edits_preserve_user_changes_and_resume_with_new_line_positions(tmp_path: Path) -> None:
    launcher = tmp_path / "launcher"
    target = tmp_path / "명시 대상 'quoted'"
    launcher.mkdir()
    target.mkdir()
    configure(launcher)
    configure(target)
    (launcher / "source.txt").write_bytes(b"unrelated caller file\n")
    file = target / "source.txt"
    file.write_bytes(b"header\r\nold\r\nfooter")

    async def scenario() -> None:
        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-I", "-m", "local_dev_mcp", "--workspace", str(target), "run"],
                cwd=launcher,
                env=base_environment(),
            ),
            read_timeout_seconds=15,
            cache=None,
        ) as session:

            async def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
                result = await session.call_tool(name, arguments)
                assert not result.is_error, result.content
                assert result.structured_content is not None
                value: dict[str, object] = dict(result.structured_content)
                assert value["project"] == str(target.resolve())
                return value

            discovered = await session.list_tools()
            assert "replace_workspace_file" in [tool.name for tool in discovered.tools]
            observed = await call("read_workspace_file", {"path": "source.txt", "start_line": 2, "max_lines": 1})
            assert isinstance(observed["read_ref"], str)
            first = await call(
                "replace_workspace_file",
                {"path": "source.txt", "read_ref": observed["read_ref"], "replacement": "agent A\r\n"},
            )
            assert first["outcome"] == "applied" and isinstance(first["read_ref"], str)
            user_content = b"user inserted\r\nheader\r\nagent A\r\nfooter"
            file.write_bytes(user_content)
            stale = await call(
                "replace_workspace_file",
                {"path": "source.txt", "read_ref": first["read_ref"], "replacement": "agent B\r\n"},
            )
            assert stale["outcome"] == "rejected" and stale["code"] == "stale_version"
            assert stale["changed"] is False and stale["next_call"] is not None
            assert file.read_bytes() == user_content

            fresh = await call("read_workspace_file", {"path": "source.txt", "start_line": 3, "max_lines": 1})
            assert fresh["content"] == "agent A\r\n" and isinstance(fresh["read_ref"], str)
            resumed = await call(
                "replace_workspace_file",
                {"path": "source.txt", "read_ref": fresh["read_ref"], "replacement": "agent B\r\n"},
            )
            assert resumed["outcome"] == "applied"
            assert resumed["version_after"] == hashlib.sha256(file.read_bytes()).hexdigest()
            assert isinstance(resumed["read_ref"], str)
            assert file.read_bytes() == b"user inserted\r\nheader\r\nagent B\r\nfooter"

    asyncio.run(scenario())
    assert (launcher / "source.txt").read_bytes() == b"unrelated caller file\n"
