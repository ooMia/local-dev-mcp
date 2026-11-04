"""Exercise tool definitions through the actual MCP invocation interface."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

import pytest
from fastmcp import Client, FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError, ValidationError
from mcp.types import TextContent, ToolAnnotations

from local_dev_mcp import cli
from local_dev_mcp.core import create_server
from local_dev_mcp.errors import ExitCode, ToolRegistrationError
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import GET_PROJECT_HEAD
from local_dev_mcp.tools.registry import ToolDefinition, register_tools, safe_execution


class EchoResult(TypedDict):
    project: str
    label: str


@dataclass
class EchoTool:
    policy: Policy
    calls: list[Path]

    async def execute(self, label: str = "hello") -> EchoResult:
        self.calls.append(self.policy.root)
        return {"project": str(self.policy.root), "label": label}


def echo_definition(calls: list[Path], commands: tuple[str, ...] = ()) -> ToolDefinition:
    def create_runtime(policy: Policy) -> EchoTool:
        return EchoTool(policy, calls)

    return ToolDefinition(
        name="echo_project",
        description="A Python-only test feature with a typed input.",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
        required_commands=commands,
        create_runtime=create_runtime,
    )


def test_python_feature_preserves_schema_and_needs_no_binary(tmp_path: Path) -> None:
    calls: list[Path] = []
    definition = echo_definition(calls)
    policy = Policy(tmp_path, (tmp_path,), ())
    server = FastMCP("test-features", strict_input_validation=True, mask_error_details=True, on_duplicate="error")
    register_tools(server, policy, (definition,), policy)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.list_tools()
            tool = result[0]
            assert tool.name == definition.name
            assert tool.description == definition.description
            assert tool.annotations == definition.annotations
            assert tool.input_schema["additionalProperties"] is False
            assert tool.input_schema["properties"] == {"label": {"default": "hello", "type": "string"}}
            assert tool.output_schema is not None
            assert set(tool.output_schema["properties"]) == {"project", "label"}
            reply = await client.call_tool("echo_project", {"label": "typed"})
            assert not reply.is_error
            assert reply.structured_content == {"project": str(tmp_path), "label": "typed"}

    asyncio.run(scenario())
    assert calls == [tmp_path]


@pytest.mark.parametrize("deny_path", [False, True])
def test_policy_denial_stops_body_but_preserves_discovery(tmp_path: Path, deny_path: bool) -> None:
    calls: list[Path] = []
    policy = Policy(tmp_path, () if deny_path else (tmp_path,), ())
    server = FastMCP("test-denial", strict_input_validation=True, mask_error_details=True, on_duplicate="error")
    register_tools(server, policy, (echo_definition(calls, commands=("git",)),), policy)

    async def scenario() -> None:
        async with Client(server) as client:
            assert [tool.name for tool in (await client.list_tools())] == ["echo_project"]
            result = await client.call_tool("echo_project", raise_on_error=False)
            assert result.is_error
            assert isinstance(result.content[0], TextContent)
            expected = "Project directory is not allowed" if deny_path else "git is not allowed"
            assert expected in result.content[0].text

    asyncio.run(scenario())
    assert calls == []


def test_authorization_runs_on_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    checks: list[str] = []
    policy = Policy(tmp_path, (tmp_path,), ("git",))
    server = FastMCP("test-recheck", strict_input_validation=True, mask_error_details=True, on_duplicate="error")
    register_tools(server, policy, (echo_definition(calls, commands=("git",)),), policy)

    def require_command(self: Policy, command: str) -> None:
        assert self is policy
        checks.append(command)
        if len(checks) > 1:
            raise PermissionError("Test policy refusal on the second invocation.")

    monkeypatch.setattr(Policy, "require_command", require_command)

    async def scenario() -> None:
        async with Client(server) as client:
            assert not (await client.call_tool("echo_project", raise_on_error=False)).is_error
            assert (await client.call_tool("echo_project", raise_on_error=False)).is_error

    asyncio.run(scenario())
    assert checks == ["git", "git"]
    assert calls == [tmp_path]


def test_shared_definition_has_separate_runtime_policy_per_server(tmp_path: Path) -> None:
    calls: list[Path] = []
    definition = echo_definition(calls)
    roots = (tmp_path / "first", tmp_path / "second")
    servers: list[FastMCP] = []
    for root in roots:
        root.mkdir()
        server = FastMCP("test-isolation", strict_input_validation=True, mask_error_details=True, on_duplicate="error")
        register_tools(server, Policy(root, (root,), ()), (definition,), Policy(root, (root,), ()))
        servers.append(server)

    async def scenario() -> None:
        async with Client(servers[0]) as first, Client(servers[1]) as second:
            assert (await first.call_tool("echo_project")).structured_content == {
                "project": str(roots[0]),
                "label": "hello",
            }
            assert (await second.call_tool("echo_project")).structured_content == {
                "project": str(roots[1]),
                "label": "hello",
            }
            assert (await first.call_tool("echo_project")).structured_content == {
                "project": str(roots[0]),
                "label": "hello",
            }

    asyncio.run(scenario())
    assert calls == [roots[0], roots[1], roots[0]]


def test_duplicate_names_fail_before_runtime_creation_or_registration(tmp_path: Path) -> None:
    created: list[Path] = []

    def create_runtime(policy: Policy) -> EchoTool:
        created.append(policy.root)
        return EchoTool(policy, [])

    definition = replace(echo_definition([]), create_runtime=create_runtime)
    server = FastMCP("test-duplicates", strict_input_validation=True, mask_error_details=True, on_duplicate="error")
    with pytest.raises(ToolRegistrationError):
        register_tools(
            server, Policy(tmp_path, (tmp_path,), ()), (definition, definition), Policy(tmp_path, (tmp_path,), ())
        )
    assert created == []
    assert asyncio.run(server.list_tools()) == []


def test_duplicate_definition_is_safe_general_cli_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = echo_definition([])

    def run() -> int:
        register_tools(
            FastMCP("test-duplicates", strict_input_validation=True, mask_error_details=True, on_duplicate="error"),
            Policy(tmp_path, (tmp_path,), ()),
            (definition, definition),
            Policy(tmp_path, (tmp_path,), ()),
        )
        return ExitCode.SUCCESS

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main() == ExitCode.GENERAL
    diagnostic = capsys.readouterr().err
    assert "ToolRegistrationError" in diagnostic
    assert "Traceback" not in diagnostic


def test_head_public_metadata_and_empty_input_are_preserved(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n")
    server = create_server(Policy(tmp_path, (tmp_path,), ("git",)))
    tool = asyncio.run(server.list_tools())[0]
    assert tool.name == GET_PROJECT_HEAD.name == "get_project_head"
    assert tool.description == GET_PROJECT_HEAD.description
    assert tool.parameters["properties"] == {}
    assert tool.annotations == GET_PROJECT_HEAD.annotations
    assert tool.output_schema is not None
    assert set(tool.output_schema["properties"]) == {"project", "head", "observed_at"}


@dataclass
class StrictInputTool:
    calls: list[tuple[bool, list[str]]]

    async def execute(self, paths: list[str], staged: bool = False) -> EchoResult:
        self.calls.append((staged, paths))
        return {"project": ".", "label": "accepted"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"paths": ["a"], "staged": "yes"},
        {"paths": ["a"], "staged": 1},
        {"paths": '["a"]'},
        {"paths": [1]},
        {"paths": ["a"], "unknown": True},
        {},
    ],
)
def test_strict_raw_arguments_rejected_before_body(tmp_path: Path, arguments: dict[str, object]) -> None:
    calls: list[tuple[bool, list[str]]] = []

    def create_runtime(policy: Policy) -> StrictInputTool:
        return StrictInputTool(calls)

    definition = replace(echo_definition([]), create_runtime=create_runtime)
    server = FastMCP("test-strict", strict_input_validation=True, mask_error_details=True)
    register_tools(server, Policy(tmp_path, (tmp_path,), ()), (definition,), Policy(tmp_path, (tmp_path,), ()))

    async def scenario() -> None:
        async with Client(server) as client:
            tool = (await client.list_tools())[0]
            assert tool.input_schema["additionalProperties"] is False
            assert tool.input_schema["required"] == ["paths"]
            assert tool.input_schema["properties"]["staged"]["type"] == "boolean"
            result = await client.call_tool("echo_project", arguments, raise_on_error=False)
            assert result.is_error
        with pytest.raises(ValidationError):
            await server.call_tool("echo_project", arguments)

    asyncio.run(scenario())
    assert calls == []


def test_diff_defaults_and_null_remain_valid_inputs(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n")
    server = create_server(Policy(tmp_path, (tmp_path,), ("git",)))

    async def scenario() -> None:
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            diff = tools["get_project_diff"].input_schema
            assert diff["additionalProperties"] is False
            assert diff["properties"]["staged"]["default"] is False
            assert diff["properties"]["paths"]["default"] is None
            for arguments in ({}, {"paths": None}):
                result = await client.call_tool("get_project_diff", arguments, raise_on_error=False)
                assert result.is_error
                assert isinstance(result.content[0], TextContent)
                assert "Locate Git working tree" in result.content[0].text

    asyncio.run(scenario())


def test_unexpected_tool_error_is_safe_in_response_and_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    sentinel = "credential-value-must-not-leak"

    @dataclass
    class FailingTool:
        async def execute(self) -> EchoResult:
            raise RuntimeError(sentinel)

    def create_runtime(policy: Policy) -> FailingTool:
        return FailingTool()

    definition = replace(echo_definition([]), create_runtime=create_runtime)
    server = FastMCP("test-safe-error", strict_input_validation=True, mask_error_details=True)
    register_tools(server, Policy(tmp_path, (tmp_path,), ()), (definition,), Policy(tmp_path, (tmp_path,), ()))

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool("echo_project", raise_on_error=False)
            assert result.is_error
            assert sentinel not in str(result.content)
        with pytest.raises(ToolError) as error:
            await server.call_tool("echo_project")
        assert sentinel not in str(error.value)

    asyncio.run(scenario())
    assert sentinel not in caplog.text


@dataclass(frozen=True)
class PreparedLabel:
    label: str


@dataclass(frozen=True)
class PreparedTool:
    policy: Policy
    provider: Callable[[], Awaitable[PreparedLabel]]
    calls: list[Path]

    @property
    def execute(self) -> Callable[..., Awaitable[EchoResult]]:
        provide = safe_execution(self.provider)

        async def invoke(label: str = "hello", prepared: PreparedLabel = Depends(provide)) -> EchoResult:
            self.calls.append(self.policy.root)
            return {"project": str(self.policy.root), "label": f"{label}:{prepared.label}"}

        return invoke


@pytest.mark.parametrize("denial", ["path", "command", "input", "prepare"])
def test_prepare_is_after_validation_and_authorization_and_failure_stops_body(tmp_path: Path, denial: str) -> None:
    calls: list[Path] = []
    events: list[str] = []

    async def prepare() -> PreparedLabel:
        events.append("prepare")
        raise ToolError("Preparation intentionally unavailable.")

    def create_runtime(policy: Policy) -> PreparedTool:
        return PreparedTool(policy, prepare, calls)

    definition = replace(echo_definition(calls, ("git",)), create_runtime=create_runtime)
    policy = Policy(tmp_path, () if denial == "path" else (tmp_path,), () if denial == "command" else ("git",))
    server = FastMCP("prepare-order", strict_input_validation=True)
    register_tools(server, policy, (definition,), policy)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool(
                "echo_project", {"label": 1} if denial == "input" else {}, raise_on_error=False
            )
            assert result.is_error
            assert isinstance(result.content[0], TextContent)
            message = result.content[0].text
            if denial in ("path", "command"):
                assert "not allowed" in message
            elif denial == "input":
                assert "Preparation intentionally unavailable" not in message
            else:
                assert "Preparation intentionally unavailable" in message

    asyncio.run(scenario())
    assert calls == []
    assert events == (["prepare"] if denial == "prepare" else [])


def test_prepared_values_are_isolated_between_concurrent_and_subsequent_requests(tmp_path: Path) -> None:
    counter = 0

    async def prepare() -> PreparedLabel:
        nonlocal counter
        counter += 1
        value = PreparedLabel(str(counter))
        await asyncio.sleep(0)
        return value

    def create_runtime(policy: Policy) -> PreparedTool:
        return PreparedTool(policy, prepare, [])

    definition = replace(echo_definition([]), create_runtime=create_runtime)
    server = FastMCP("prepare-isolation", strict_input_validation=True)
    register_tools(server, Policy(tmp_path, (tmp_path,), ()), (definition,), Policy(tmp_path, (tmp_path,), ()))

    async def scenario() -> None:
        async with Client(server) as client:
            tool = (await client.list_tools())[0]
            assert set(tool.input_schema["properties"]) == {"label"}
            first, second = await asyncio.gather(
                client.call_tool("echo_project", {"label": "a"}),
                client.call_tool("echo_project", {"label": "b"}),
            )
            assert first.structured_content == {"project": str(tmp_path), "label": "a:1"}
            assert second.structured_content == {"project": str(tmp_path), "label": "b:2"}
            third = await client.call_tool("echo_project")
            assert third.structured_content == {"project": str(tmp_path), "label": "hello:3"}

    asyncio.run(scenario())


def test_dependency_failure_is_safe_in_response_and_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    sentinel = "credential-provider-must-not-leak"
    calls: list[Path] = []

    async def prepare() -> PreparedLabel:
        raise RuntimeError(sentinel)

    def create_runtime(policy: Policy) -> PreparedTool:
        return PreparedTool(policy, prepare, calls)

    definition = replace(echo_definition([]), create_runtime=create_runtime)
    policy = Policy(tmp_path, (tmp_path,), ())
    server = FastMCP("safe-provider", strict_input_validation=True)
    register_tools(server, policy, (definition,), policy)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool("echo_project", raise_on_error=False)
            assert result.is_error
            assert sentinel not in str(result.content)
            assert "RuntimeError" in str(result.content)

    asyncio.run(scenario())
    assert calls == []
    assert sentinel not in caplog.text


def test_permission_denial_precedes_invalid_arguments(tmp_path: Path) -> None:
    policy = Policy(tmp_path, (tmp_path,), ())
    server = FastMCP("permission-first", strict_input_validation=True)
    register_tools(server, policy, (echo_definition([], ("git",)),), policy)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool("echo_project", {"label": 1}, raise_on_error=False)
            assert result.is_error
            assert "not allowed" in str(result.content)

    asyncio.run(scenario())


def test_dependency_settings_are_isolated_between_servers(tmp_path: Path) -> None:
    def create_runtime(policy: Policy) -> PreparedTool:
        async def provide() -> PreparedLabel:
            await asyncio.sleep(0)
            return PreparedLabel(policy.root.name)

        return PreparedTool(policy, provide, [])

    definition = replace(echo_definition([]), create_runtime=create_runtime)
    servers: list[FastMCP] = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        policy = Policy(root, (root,), ())
        server = FastMCP(name, strict_input_validation=True)
        register_tools(server, policy, (definition,), policy)
        servers.append(server)

    async def scenario() -> None:
        async with Client(servers[0]) as first, Client(servers[1]) as second:
            results = await asyncio.gather(first.call_tool("echo_project"), second.call_tool("echo_project"))
            assert [result.structured_content for result in results] == [
                {"project": str(tmp_path / "first"), "label": "hello:first"},
                {"project": str(tmp_path / "second"), "label": "hello:second"},
            ]

    asyncio.run(scenario())


def test_safe_execution_preserves_cancellation() -> None:
    @safe_execution
    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())


def test_commit_dependency_is_hidden_from_public_schema(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n")
    server = create_server(Policy(tmp_path, (tmp_path,), ("git",)))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["commit_project_changes"].parameters
    assert set(schema["properties"]) == {"message"}
    assert schema["required"] == ["message"]
    assert schema["properties"]["message"]["minLength"] == 1
    assert schema["additionalProperties"] is False
