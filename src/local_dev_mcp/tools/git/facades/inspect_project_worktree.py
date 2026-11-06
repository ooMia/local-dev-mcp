"""Observe owned worktree readiness, including incomplete preparation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .. import _worktrees
from .._prepare import git_readiness
from ._refs import BranchName

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class InspectProjectWorktreeTool:
    settings: ServerSettings

    async def execute(self, branch: BranchName) -> _worktrees.WorktreeState:
        return await _worktrees.inspect(self.settings, branch)


INSPECT_PROJECT_WORKTREE: Final = ToolDefinition(
    name="inspect_project_worktree",
    description=(
        "Observe a fixed managed worktree as absent, partial or ready, with branch SHA and clean state. "
        "Does not create, adopt, switch or remove worktrees. Partial states require inspection before further writes."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=InspectProjectWorktreeTool,
    readiness=git_readiness,
)
