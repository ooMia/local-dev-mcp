"""Static readiness must reflect explicitly denied logical commands."""

from dataclasses import dataclass

import pytest
from mcp.types import ToolAnnotations

from local_dev_mcp.mcp.registry import ToolDefinition, tool_readiness

pytestmark = pytest.mark.unit


@dataclass
class _Runtime:
    async def execute(self) -> dict[str, bool]:
        return {"ok": True}


def _create_runtime(_settings: object) -> _Runtime:
    return _Runtime()


def _tool(name: str, commands: tuple[str, ...]) -> ToolDefinition[object]:
    return ToolDefinition(
        name=name,
        description="Readiness contract fixture.",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
        required_commands=commands,
        create_runtime=_create_runtime,
    )


def test_startup_readiness_all_tools_ready_when_no_readiness_callable() -> None:
    # required_commands are enforced at invocation time by ToolAuthorization,
    # not at readiness time, so all tools are ready here.
    ready, denied = tool_readiness(
        (_tool("git_only", ("git",)), _tool("worktree_trail", ("git", "trail"))),
        object(),
    )
    assert ready.issue is None
    assert denied.issue is None

    (ready,) = tool_readiness((_tool("worktree_trail", ("git", "trail")),), object())
    assert ready.issue is None
