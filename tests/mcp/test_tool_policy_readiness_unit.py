"""Static readiness must reflect explicitly denied logical commands."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp.types import ToolAnnotations

from local_dev_mcp.mcp.registry import ToolDefinition, tool_readiness
from local_dev_mcp.settings import Policy

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


def test_startup_readiness_marks_denied_command_unavailable(tmp_path: Path) -> None:
    policy = Policy(tmp_path, (tmp_path,), ("git",))
    ready, denied = tool_readiness(
        (_tool("git_only", ("git",)), _tool("worktree_trail", ("git", "trail"))),
        object(),
        policy,
    )

    assert ready.issue is None
    assert denied.name == "worktree_trail"
    assert denied.issue is not None
    public = denied.issue.public()
    assert public["target"] == "trail"
    assert public["reason"] == "System command trail is not allowed by config.toml."
    assert public["restart_required"] is True


def test_startup_readiness_honors_permission_grant(tmp_path: Path) -> None:
    policy = Policy(tmp_path, (tmp_path,), ("git", "trail"))
    (ready,) = tool_readiness((_tool("worktree_trail", ("git", "trail")),), object(), policy)
    assert ready.issue is None
