"""Public MCP indexing boundary: root isolation and write/refresh separation."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager, TrailProcessResult
from tests.support.mcp import success
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def test_index_tools_use_configured_root_without_path_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[filesystem]\nallowed_paths=["."]\n[system]\nallowed_commands=["trail"]\n',
        encoding="utf-8",
    )
    invoked: list[tuple[str, ...]] = []

    def fake_binary(self: IndexManager) -> Path:
        return Path("/mock/trail")

    monkeypatch.setattr(IndexManager, "_binary", fake_binary)

    async def fake(self: IndexManager, binary: Path, args: tuple[str, ...], timeout: float) -> TrailProcessResult:
        invoked.append(args)
        return TrailProcessResult(0, "graft check: OK\ngraph check: STALE\n")

    monkeypatch.setattr(IndexManager, "_run", fake)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            tools = {item.name: item for item in await client.list_tools()}
            assert tools["get_project_index_status"].input_schema["properties"] == {}
            assert tools["refresh_project_index"].input_schema["properties"] == {}
            status = await success(client, "get_project_index_status")
            assert status["project"] == str(tmp_path.resolve())
            assert status["structural_state"] == "fresh"
            assert status["deep_state"] == "stale"
            assert status["ready_for_structural_search"] is True
            refreshed = await success(client, "refresh_project_index")
            assert refreshed["outcome"] == "already_fresh"
            assert refreshed["refresh_performed"] is False

    asyncio.run(scenario())
    assert invoked == [("build", "--check", str(tmp_path.resolve()))] * 2


def test_workspace_mutations_only_notify_real_new_edits(tmp_path: Path) -> None:
    from local_dev_mcp.tools.workspace._files import WorkspaceFiles

    events: list[str] = []
    workspace = WorkspaceFiles(Policy(tmp_path, (tmp_path,), ()), change_callback=lambda: events.append("edit"))

    async def scenario() -> None:
        created = await workspace.create_file("test.py", "one\n", "mutation-a")
        assert created.outcome == "applied"
        assert len(events) == 1
        replayed = await workspace.create_file("test.py", "one\n", "mutation-a")
        assert replayed.replayed and len(events) == 1
        rejected = await workspace.create_file("test.py", "other\n", "mutation-b")
        assert rejected.outcome == "rejected" and len(events) == 1
        observed = await workspace.read_file("test.py", 1, 10)
        assert observed.read_ref
        updated = await workspace.replace_file("test.py", observed.read_ref, "two\n")
        assert updated.outcome == "applied" and len(events) == 2
        assert updated.read_ref
        deleted = await workspace.delete_file("test.py", updated.read_ref, "mutation-c")
        assert deleted.outcome == "applied" and len(events) == 3
        again = await workspace.delete_file("test.py", updated.read_ref, "mutation-c")
        assert again.replayed and len(events) == 3
        workspace.clear()

    asyncio.run(scenario())


def test_optional_index_failure_never_reverses_file_write(tmp_path: Path) -> None:
    from local_dev_mcp.tools.workspace._files import WorkspaceFiles

    def fail() -> None:
        raise RuntimeError("simulated index failure")

    workspace = WorkspaceFiles(Policy(tmp_path, (tmp_path,), ()), change_callback=fail)

    async def scenario() -> None:
        result = await workspace.create_file("source.py", "safe\n", "mutation-r")
        assert result.outcome == "applied"
        assert (tmp_path / "source.py").read_text() == "safe\n"
        workspace.clear()

    asyncio.run(scenario())
