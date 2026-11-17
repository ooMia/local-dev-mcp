import asyncio
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import TypeAdapter
from shellous import sh

from local_dev_mcp.mcp.server import TOOLS, create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.get_tool_readiness import ReadinessReport
from local_dev_mcp.tools.git import command_environment
from tests.support.git import configure_identity
from tests.support.signing import SigningSetup
from tests.support.workspace import WorkspaceConfigFactory

pytestmark = pytest.mark.integration


async def git(root: Path, *args: str) -> str:
    return (
        await sh("git", "-C", root, *args).set(
            inherit_env=False,
            env=command_environment(),
            timeout=10,
        )
    ).strip()


def client(root: Path) -> Client:
    policy = Policy.load(root)
    fastmcp_server = create_server(policy)
    return Client(
        fastmcp_server._mcp_server,  # type: ignore[reportPrivateUsage]
        read_timeout_seconds=15,
        cache=None,
    )


def test_live_head_changes_without_server_restart(tmp_path: Path, workspace_config: WorkspaceConfigFactory) -> None:
    workspace_config(commands='["git"]')

    async def scenario() -> None:
        await git(tmp_path, "init", "--quiet")
        await git(
            tmp_path,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "first",
        )
        first = await git(tmp_path, "rev-parse", "HEAD")
        async with client(tmp_path) as session:
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == [tool.name for tool in TOOLS]
            assert tools.tools[0].annotations is not None
            assert tools.tools[0].annotations.read_only_hint is True
            result = await session.call_tool("get_project_head")
            assert not result.is_error
            assert result.structured_content is not None
            assert result.structured_content["head"] == first
            await git(
                tmp_path,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "second",
            )
            second = await git(tmp_path, "rev-parse", "HEAD")
            result = await session.call_tool("get_project_head")
            assert not result.is_error
            assert result.structured_content is not None
            assert result.structured_content["head"] == second != first
            await git(tmp_path, "checkout", "--quiet", "--detach", first)
            result = await session.call_tool("get_project_head")
            assert result.structured_content is not None
            assert result.structured_content["head"] == first

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("commands", "paths", "message"),
    [
        ('["git"]', "[]", "project directory"),
    ],
)
def test_policy_denial_prevents_stdio_start(
    tmp_path: Path, commands: str, paths: str, message: str, workspace_config: WorkspaceConfigFactory
) -> None:
    workspace_config(commands, paths)

    async def scenario() -> None:
        result = await sh.result(sys.executable, "-I", "-m", "local_dev_mcp", "run").set(
            cwd=tmp_path, inherit_env=False, env=command_environment(), timeout=10
        )
        assert result.exit_code == 3
        assert message in result.error
        assert result.output == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("initialized", [False, True])
def test_no_repository_or_unborn_head_is_mcp_error(
    tmp_path: Path, initialized: bool, workspace_config: WorkspaceConfigFactory
) -> None:
    workspace_config(commands='["git"]')

    async def scenario() -> None:
        if initialized:
            await git(tmp_path, "init", "--quiet")
        async with client(tmp_path) as session:
            result = await session.call_tool("get_project_head")
            assert result.is_error
            assert isinstance(result.content[0], TextContent)
            message = "at least one commit" if initialized else "Locate Git working tree"
            assert message in result.content[0].text

    asyncio.run(scenario())


def test_stdio_stage_commit_and_head_observation(
    tmp_path: Path, signing_setup: SigningSetup, workspace_config: WorkspaceConfigFactory
) -> None:

    workspace_config(commands='["git", "gpg"]')
    (tmp_path / ".env").write_text(
        'GIT_USER_NAME="Stdio Test"\nGIT_USER_EMAIL=stdio@example.invalid\n' + signing_setup.dotenv()
    )

    async def scenario() -> None:
        await git(tmp_path, "init", "--quiet")
        (tmp_path / "data.txt").write_text("stdio payload\n")
        async with client(tmp_path) as session:
            stage = await session.call_tool("stage_project_paths", {"paths": ["data.txt"]})
            assert not stage.is_error
            diff = await session.call_tool("get_project_diff", {"staged": True})
            assert not diff.is_error
            assert diff.structured_content is not None
            assert "+stdio payload" in diff.structured_content["patch"]
            commit = await session.call_tool("commit_project_changes", {"message": "stdio commit"})
            assert not commit.is_error
            assert commit.structured_content is not None
            assert commit.structured_content["head_observed"] is True
            assert commit.structured_content["signature_verified"] is True
            head = await session.call_tool("get_project_head")
            assert head.structured_content is not None
            assert head.structured_content["head"] == commit.structured_content["head_after"]
        assert await git(tmp_path, "show", "HEAD:data.txt") == "stdio payload"
        assert await git(tmp_path, "ls-files") == "data.txt"

    asyncio.run(scenario())


def test_stdio_settings_stay_fixed_and_invalid_commit_is_blocked_after_restart(
    tmp_path: Path, workspace_config: WorkspaceConfigFactory
) -> None:
    workspace_config(commands='["git"]')
    configure_identity(tmp_path, name="Test", email="test@example.invalid")

    async def scenario() -> None:
        await git(tmp_path, "init", "--quiet")
        (tmp_path / "data").write_text("snapshot over stdio")
        async with client(tmp_path) as session:
            # Initialization has completed before the file is changed.
            await session.list_tools()
            (tmp_path / ".env").write_text(
                "GIT_USER_NAME=Changed\nGIT_USER_EMAIL=changed@example.invalid\nGIT_COMMIT_SIGN=true\n"
            )
            assert not (await session.call_tool("stage_project_paths", {"paths": ["data"]})).is_error
            result = await session.call_tool("commit_project_changes", {"message": "original snapshot"})
            assert not result.is_error
            assert result.structured_content is not None
            assert result.structured_content["signing_requested"] is False
            assert result.structured_content["signature_verified"] is None
        assert await git(tmp_path, "show", "-s", "--format=%an") == "Test"
        async with client(tmp_path) as session:
            status = await session.call_tool("get_project_status")
            assert not status.is_error
            blocked = await session.call_tool("commit_project_changes", {"message": "blocked"})
            assert blocked.is_error
            assert isinstance(blocked.content[0], TextContent)
            assert "gpg is not allowed" in blocked.content[0].text

    asyncio.run(scenario())


def test_stdio_strict_schema_rejects_invalid_arguments_without_staging(
    tmp_path: Path, workspace_config: WorkspaceConfigFactory
) -> None:
    """An independent SDK client checks the FastMCP server's wire contract."""
    workspace_config(commands='["git"]')
    configure_identity(tmp_path, name="Test", email="test@example.invalid")
    sentinel = "credential-value-must-not-be-echoed"

    async def scenario() -> None:
        await git(tmp_path, "init", "--quiet")
        (tmp_path / "selected.txt").write_text("selected\n")
        (tmp_path / "untouched.txt").write_text("untouched\n")
        async with client(tmp_path) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert all(tool.input_schema["additionalProperties"] is False for tool in tools.values())
            stage_schema = tools["stage_project_paths"].input_schema
            assert stage_schema["required"] == ["paths"]
            assert stage_schema["properties"]["paths"]["minItems"] == 1
            assert "description" in stage_schema["properties"]["paths"]
            invalid_calls: tuple[tuple[str, dict[str, object]], ...] = (
                ("stage_project_paths", {}),
                ("stage_project_paths", {"paths": []}),
                ("stage_project_paths", {"paths": '["selected.txt"]'}),
                ("stage_project_paths", {"paths": ["selected.txt"], "unexpected": sentinel}),
                ("get_project_diff", {"staged": sentinel}),
                ("get_project_diff", {"staged": 1}),
                ("get_project_head", {"unexpected": sentinel}),
                ("commit_project_changes", {"message": 1}),
            )
            for name, arguments in invalid_calls:
                result = await session.call_tool(name, arguments)
                assert result.is_error
                assert isinstance(result.content[0], TextContent)
                assert "Invalid tool arguments" in result.content[0].text
                assert sentinel not in result.content[0].text
                assert await git(tmp_path, "ls-files") == ""
            # A bad request must not poison the server or widen a later scope.
            result = await session.call_tool("stage_project_paths", {"paths": ["selected.txt"]})
            assert not result.is_error
            assert await git(tmp_path, "ls-files") == "selected.txt"
            for arguments in ({}, {"paths": None}):
                result = await session.call_tool("get_project_diff", arguments)
                assert not result.is_error

    asyncio.run(scenario())


def test_stdio_does_not_load_fastmcp_settings_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_config: WorkspaceConfigFactory
) -> None:
    workspace_config(commands='["git"]')
    # Either automatic dotenv loading or inherited FASTMCP_* configuration
    # would reject this invalid log level before the server could start.
    (tmp_path / ".env").write_text(
        "FASTMCP_LOG_LEVEL=INVALID\nGIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n"
    )
    monkeypatch.setenv("FASTMCP_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INVALID")

    async def scenario() -> None:
        await git(tmp_path, "init", "--quiet")
        async with client(tmp_path) as session:
            tools = await session.list_tools()
            assert len(tools.tools) == len(TOOLS)
            result = await session.call_tool("get_project_status")
            assert not result.is_error

    asyncio.run(scenario())


@pytest.mark.parametrize("git_available", [True, False])
def test_stdio_starts_with_unavailable_features_and_reports_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_config: WorkspaceConfigFactory, git_available: bool
) -> None:
    workspace_config(commands='["git"]')

    async def scenario() -> None:
        if git_available:
            await git(tmp_path, "init", "--quiet")
            (tmp_path / "data").write_text("stdio without identity")
        else:
            monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
        async with client(tmp_path) as session:
            result = await session.call_tool("get_tool_readiness")
            assert not result.is_error
            report = TypeAdapter(ReadinessReport).validate_python(result.structured_content)
            assert report["scope"] == "startup_snapshot_only"
            states = {state["name"]: state for state in report["tools"]}
            assert states["get_tool_readiness"]["ready"]
            assert states["get_project_status"]["ready"] is git_available
            assert not states["commit_project_changes"]["ready"]
            result = await session.call_tool("commit_project_changes", {"message": 1})
            assert result.is_error and isinstance(result.content[0], TextContent)
            assert "Invalid tool arguments" not in result.content[0].text
            assert "restart" in result.content[0].text.lower()
            if git_available:
                for name, arguments in (
                    ("get_project_status", {}),
                    ("stage_project_paths", {"paths": ["data"]}),
                    ("unstage_project_paths", {"paths": ["data"]}),
                ):
                    result = await session.call_tool(name, arguments)
                    assert not result.is_error, result.content

    asyncio.run(scenario())


def test_stdio_file_workflow_without_git_or_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_config: WorkspaceConfigFactory
) -> None:
    import hashlib
    import json

    workspace_config(commands="[]")
    (tmp_path / ".env").write_text("PRIVATE_FILE_CANARY=do-not-return\n")
    (tmp_path / "src").mkdir()
    source = b'def greet():\r\n\treturn "Hello"\r\n'
    (tmp_path / "src/greet.py").write_bytes(source)
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries"))

    async def scenario() -> None:
        async with client(tmp_path) as session:
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == [tool.name for tool in TOOLS]
            for name, args in (
                ("list_workspace_entries", {}),
                ("find_workspace_paths", {"pattern": "**/*.py"}),
                ("search_workspace_text", {"query": "Hello", "output": "content"}),
            ):
                result = await session.call_tool(name, args)
                assert not result.is_error
                assert result.structured_content is not None
                assert "PRIVATE_FILE_CANARY" not in json.dumps(result.structured_content)
            result = await session.call_tool("read_workspace_file", {"path": "src/greet.py", "start_line": 2})
            assert not result.is_error and result.structured_content is not None
            read = result.structured_content
            assert read["content"] == '\treturn "Hello"\r\n'
            assert read["version"] == hashlib.sha256(source).hexdigest()
            assert read["read_ref"]
            assert "Hello" not in str(result.content)
            blocked = await session.call_tool("read_workspace_file", {"path": ".env"})
            assert blocked.is_error and "do-not-return" not in str(blocked.content)

    asyncio.run(scenario())
