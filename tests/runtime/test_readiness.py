"""Static readiness through the same MCP interface used by Chat."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client
from pydantic import TypeAdapter

from local_dev_mcp.core import TOOLS, create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.workspace.get_tool_readiness import ReadinessReport
from tests.support.git import WorkspaceConfigFactory, WorkspaceFactory, failure, git, success


def test_missing_identity_keeps_git_workflow_and_blocks_commit_before_validation(
    tmp_path: Path, stdio_workspace: WorkspaceConfigFactory, workspace_factory: WorkspaceFactory
) -> None:
    stdio_workspace()

    async def scenario() -> None:
        await workspace_factory(identity=False)
        (tmp_path / ".env").write_text("")
        (tmp_path / "data").write_text("payload")
        async with Client(create_server(Policy.load(tmp_path))) as session:
            assert [tool.name for tool in await session.list_tools()] == [tool.name for tool in TOOLS]
            report = TypeAdapter(ReadinessReport).validate_python(await success(session, "get_tool_readiness"))
            states = {item["name"]: item for item in report["tools"]}
            assert states["get_tool_readiness"]["ready"]
            assert states["stage_project_paths"]["ready"]
            issue = states["commit_project_changes"]["issue"]
            assert issue is not None and issue["restart_required"]
            assert issue["target"] == "GIT_USER_NAME"
            await success(session, "stage_project_paths", {"paths": ["data"]})
            await success(session, "get_project_status")
            await success(session, "get_project_diff", {"staged": True})
            error = await failure(session, "commit_project_changes", {"message": 1})
            assert "Required commit identity is missing" in error
            assert "Invalid tool arguments" not in error
            assert await git(tmp_path, "ls-files") == "data"
            await success(session, "unstage_project_paths", {"paths": ["data"]})
            assert await git(tmp_path, "ls-files") == ""

    asyncio.run(scenario())


def test_missing_git_keeps_status_available_and_snapshot_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdio_workspace: WorkspaceConfigFactory
) -> None:
    stdio_workspace()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    server = create_server(Policy.load(tmp_path))

    async def scenario() -> None:
        async with Client(server) as session:
            first = TypeAdapter(ReadinessReport).validate_python(await success(session, "get_tool_readiness"))
            assert all(not tool["ready"] for tool in first["tools"] if tool["name"] != "get_tool_readiness")
            error = await failure(session, "get_project_head")
            assert "executable is missing" in error
            (tmp_path / ".env").write_text("GIT_USER_NAME=Changed\nGIT_COMMIT_SIGN=true\n")
            again = TypeAdapter(ReadinessReport).validate_python(await success(session, "get_tool_readiness"))
            assert again == first
            assert "Changed" not in str(again)

    asyncio.run(scenario())


def test_readiness_does_not_probe_and_restart_rebuilds_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdio_workspace: WorkspaceConfigFactory
) -> None:
    import local_dev_mcp.tools.git._prepare as _prepare
    from local_dev_mcp import runtime

    stdio_workspace()
    (tmp_path / ".env").write_text("")
    policy = Policy.load(tmp_path)
    unavailable = create_server(policy)
    (tmp_path / ".env").write_text("GIT_USER_NAME=Private Identity\nGIT_USER_EMAIL=private@example.invalid\n")
    ready = create_server(policy)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Readiness must not reload or probe")

    monkeypatch.setattr(runtime, "read_dotenv", forbidden)
    monkeypatch.setattr(_prepare, "prepare_git_commit", forbidden)

    async def scenario() -> None:
        async with Client(unavailable) as old, Client(ready) as new:
            before = await success(old, "get_tool_readiness")
            after = await success(new, "get_tool_readiness")
            old_report = TypeAdapter(ReadinessReport).validate_python(before)
            new_report = TypeAdapter(ReadinessReport).validate_python(after)
            assert old_report["tools"][-2]["ready"] is False
            assert new_report["tools"][-2]["ready"] is True
            assert old_report["snapshot_at"] != new_report["snapshot_at"]
            assert "Private Identity" not in str(after)
            assert "private@example.invalid" not in str(after)
            tools = await new.list_tools()
            status = next(tool for tool in tools if tool.name == "get_tool_readiness")
            assert status.input_schema.get("properties") == {}
            assert status.annotations is not None and status.annotations.read_only_hint is True
            assert "Invalid tool arguments" in await failure(new, "get_tool_readiness", {"extra": True})

    asyncio.run(scenario())


def test_static_failure_skips_dependencies_and_direct_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdio_workspace: WorkspaceConfigFactory
) -> None:
    from fastmcp.exceptions import ToolError

    import local_dev_mcp.tools.git._prepare as _prepare
    from local_dev_mcp.core import load_settings
    from local_dev_mcp.tools.git._execution import GitExecution
    from local_dev_mcp.tools.git.facades import commit_project_changes

    stdio_workspace()
    (tmp_path / ".env").write_text("")
    policy = Policy.load(tmp_path)
    server = create_server(policy)
    settings = load_settings(policy)

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unavailable commit must not execute its provider")

    monkeypatch.setattr(commit_project_changes, "prepare_git_commit", forbidden)

    async def scenario() -> None:
        async with Client(server) as session:
            assert "Required commit identity is missing" in await failure(
                session, "commit_project_changes", {"message": False}
            )
        with pytest.raises(ToolError, match="Required commit identity is missing"):
            await _prepare.prepare_git_commit(settings)
        monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
        missing_git = load_settings(policy)
        with pytest.raises(ToolError, match="executable is missing"):
            await GitExecution(missing_git).run("status", operation="Direct unavailable Git")

    asyncio.run(scenario())


def test_policy_precedes_readiness_and_preparation_in_internal_and_mcp_calls(
    tmp_path: Path, stdio_workspace: WorkspaceConfigFactory
) -> None:
    from fastmcp.exceptions import ToolError

    from local_dev_mcp.core import load_settings
    from local_dev_mcp.tools.git._prepare import prepare_git_commit

    stdio_workspace(commands="[]")
    (tmp_path / ".env").write_text("")
    policy = Policy.load(tmp_path)
    settings = load_settings(policy)

    async def scenario() -> None:
        async with Client(create_server(policy)) as session:
            report = TypeAdapter(ReadinessReport).validate_python(await success(session, "get_tool_readiness"))
            assert all(not tool["ready"] for tool in report["tools"][:-1])
            error = await failure(session, "commit_project_changes", {"message": 0})
            assert "git is not allowed" in error
            assert "identity" not in error
        with pytest.raises(ToolError, match="git is not allowed"):
            await prepare_git_commit(settings)

    asyncio.run(scenario())


def test_shared_preparation_failure_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stdio_workspace: WorkspaceConfigFactory
) -> None:
    import logging

    stdio_workspace(commands="[]")
    with caplog.at_level(logging.WARNING, logger="local_dev_mcp"):
        create_server(Policy.load(tmp_path))
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "git" in warnings[0].getMessage()
    assert "restart" in warnings[0].getMessage().lower()


def test_unexpected_configuration_exception_remains_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdio_workspace: WorkspaceConfigFactory
) -> None:
    from local_dev_mcp import runtime

    stdio_workspace()

    def unexpected(policy: Policy) -> Path:
        raise RuntimeError("internal failure")

    monkeypatch.setattr(runtime, "git_configuration", unexpected)
    with pytest.raises(RuntimeError, match="internal failure"):
        create_server(Policy.load(tmp_path))
