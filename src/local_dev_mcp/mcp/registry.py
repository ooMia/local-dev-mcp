"""Explicit tool definitions and the shared, schema-preserving invocation path."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Protocol

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams, TextContent, ToolAnnotations
from pydantic import ValidationError as PydanticValidationError

from ..errors import PolicyError, ToolRegistrationError
from ..readiness import PreparationIssue, ToolReadiness
from ..settings import Policy


class ToolRuntime(Protocol):
    @property
    def execute(self) -> Callable[..., Awaitable[object]]:
        """FastMCP inspects each concrete method's parameter and return types.

        The heterogeneous registry does not prescribe one parameter list; keep
        that dynamic callable boundary here, rather than erasing feature types.
        """
        ...


@dataclass(frozen=True)
class ToolDefinition[Settings = Policy]:
    name: str
    description: str
    annotations: ToolAnnotations
    required_commands: tuple[str, ...] | Callable[[Settings], tuple[str, ...]]
    create_runtime: Callable[[Settings], ToolRuntime]
    readiness: Callable[[Settings], PreparationIssue | None] | None = None
    compact_result: bool = False


class SafeToolDiagnostics(Middleware):
    """Present library validation errors without echoing rejected values."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ValidationError as error:
            details: list[str] = []
            cause = error.__cause__
            if isinstance(cause, PydanticValidationError):
                # Unexpected keyword names are caller-controlled too. Only
                # include locations that start with a declared tool parameter.
                tool = (
                    await context.fastmcp_context.fastmcp.get_tool(context.message.name)
                    if context.fastmcp_context is not None
                    else None
                )
                # FastMCP's public schema mapping has dynamic values; parameter
                # names are schema keys, never copied from the rejected input.
                fields: dict[str, object] = tool.parameters.get("properties", {}) if tool is not None else {}
                for issue in cause.errors(include_input=False, include_context=False, include_url=False):
                    location = issue["loc"]
                    if location and location[0] in fields:
                        details.append(f"{location[0]}: {issue['type']}")
                    else:
                        details.append("missing or unexpected argument")
            summary = "; ".join(dict.fromkeys(details)) or "arguments do not match the input schema"
            raise ToolError(
                f"Invalid tool arguments: {summary}. Use the declared field names, JSON types and required values. "
                "Consult this tool's input schema before retrying; no tool body was executed."
            ) from None


class ToolAuthorization(Middleware):
    """Authorize registered names before FastMCP validates or resolves dependencies."""

    policy: Policy
    commands: dict[str, tuple[str, ...]]
    readiness: dict[str, PreparationIssue]
    compact_results: frozenset[str]

    def __init__(
        self,
        policy: Policy,
        commands: dict[str, tuple[str, ...]],
        readiness: tuple[ToolReadiness, ...],
        compact_results: frozenset[str],
    ) -> None:
        self.policy = policy
        self.commands = commands
        self.readiness = {state.name: state.issue for state in readiness if state.issue is not None}
        self.compact_results = compact_results

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        commands = self.commands.get(context.message.name)
        # Unknown names retain FastMCP's standard lookup error.
        if commands is not None:
            try:
                self.policy.require_project_access()
                for command in commands:
                    self.policy.require_command(command)
            except PermissionError as error:
                raise ToolError(str(error)) from None
        issue = self.readiness.get(context.message.name)
        if issue is not None:
            raise ToolError(issue.diagnostic())
        result = await call_next(context)
        if (
            context.message.name in self.compact_results
            and result.structured_content is not None
            and not result.is_error
        ):
            # FastMCP normally repeats JSON in TextContent. Keep typed output/schema,
            # but avoid sending a second full copy of source code to the client.
            summary = "Workspace result is provided in structuredContent."
            outcome = result.structured_content.get("outcome")
            if outcome in ("applied", "unchanged", "rejected", "applied_unverified"):
                operation = result.structured_content.get("operation", "edit")
                summary = f"Workspace {operation} outcome: {outcome}."
                if result.structured_content.get("replayed") is True:
                    summary += " Replayed receipt; inspect current state before new work."
                code = result.structured_content.get("code")
                guidance = result.structured_content.get("next_action")
                if isinstance(code, str):
                    summary += f" Code: {code}."
                if isinstance(guidance, str):
                    summary += " " + guidance
            elif isinstance(result.structured_content.get("job_id"), str):
                state = result.structured_content.get("state")
                job_id = result.structured_content.get("job_id")
                summary = f"Project task {job_id}: {state}."
                guidance = result.structured_content.get("next_action")
                if isinstance(guidance, str):
                    summary += " " + guidance
            result.content = [TextContent(type="text", text=summary)]
        return result


def safe_execution[**P, R](execute: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Sanitize provider/body exceptions before FastMCP's internal error logger."""

    @wraps(execute)
    async def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await execute(*args, **kwargs)
        except ToolError:
            raise
        except Exception as error:
            # FastMCP's response masking does not suppress its internal exception
            # logging. Convert unknown body errors before they reach that logger.
            raise ToolError(
                f"Tool execution failed ({type(error).__name__}). "
                "State may have changed; inspect project state before retrying."
            ) from None

    return invoke


def register_tools[Settings](
    server: FastMCP,
    policy: Policy,
    definitions: tuple[ToolDefinition[Settings], ...],
    settings: Settings,
    *,
    readiness: tuple[ToolReadiness, ...] | None = None,
) -> None:
    """Register the complete definition list once on a fresh server."""
    # Reject the entire list before any runtime is created; FastMCP's duplicate
    # policy additionally covers names already present on the server.
    names = [definition.name for definition in definitions]
    if len(names) != len(set(names)):
        raise ToolRegistrationError("Duplicate MCP tool names in tool definitions.")

    commands = {
        definition.name: definition.required_commands(settings)
        if callable(definition.required_commands)
        else definition.required_commands
        for definition in definitions
    }
    states = readiness if readiness is not None else tool_readiness(definitions, settings, policy)
    compact = frozenset(definition.name for definition in definitions if definition.compact_result)
    server.add_middleware(ToolAuthorization(policy, commands, states, compact))
    for definition in definitions:
        runtime = definition.create_runtime(settings)
        tool = Tool.from_function(
            safe_execution(runtime.execute),
            name=definition.name,
            description=definition.description,
            annotations=definition.annotations,
        )
        server.add_tool(tool)


def tool_readiness[Settings](
    definitions: tuple[ToolDefinition[Settings], ...], settings: Settings, policy: Policy
) -> tuple[ToolReadiness, ...]:
    states: list[ToolReadiness] = []
    for definition in definitions:
        commands = (
            definition.required_commands(settings)
            if callable(definition.required_commands)
            else definition.required_commands
        )
        issue = None
        for command in commands:
            try:
                policy.require_command(command)
            except PermissionError:
                issue = PreparationIssue.from_error(PolicyError.command_denied(command))
                break
        if issue is None and definition.readiness is not None:
            issue = definition.readiness(settings)
        states.append(ToolReadiness(definition.name, issue))
    return tuple(states)
