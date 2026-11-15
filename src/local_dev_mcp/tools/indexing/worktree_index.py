"""Public M4-P1 Trail status/refresh on one exact Git-registered linked worktree."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ..git.facades._refs import BranchName, ExpectedHead
from ._worktree_service import WorktreeIndexReport

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class GetProjectWorktreeIndexStatusTool:
    settings: ServerSettings

    async def execute(self, branch: BranchName, expected_head: ExpectedHead) -> WorktreeIndexReport:
        return await self.settings.worktree_indexes.execute(branch=branch, expected_head=expected_head, action="status")


@dataclass(frozen=True)
class RefreshProjectWorktreeIndexTool:
    settings: ServerSettings

    async def execute(self, branch: BranchName, expected_head: ExpectedHead) -> WorktreeIndexReport:
        return await self.settings.worktree_indexes.execute(
            branch=branch, expected_head=expected_head, action="refresh"
        )


GET_PROJECT_WORKTREE_INDEX_STATUS: Final = ToolDefinition(
    name="get_project_worktree_index_status",
    description=(
        "Check Trail structural/Deep freshness for one clean, Git-registered linked Worktrunk "
        "with an exact branch and HEAD. No arbitrary paths or writes. Revalidate registered "
        "checkout after the check; existing project-root status API is unchanged."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "trail"),
    create_runtime=GetProjectWorktreeIndexStatusTool,
)

REFRESH_PROJECT_WORKTREE_INDEX: Final = ToolDefinition(
    name="refresh_project_worktree_index",
    description=(
        "Refresh only the structural Trail graph of one clean, Git-registered linked Worktrunk "
        "at an exact branch and HEAD. Per-root server-local locking, second freshness check, "
        "Git identity/clean revalidation and no --deep. No arbitrary directory input, "
        "automatic cleanup or changes to existing project-root indexing."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write", "trail"),
    create_runtime=RefreshProjectWorktreeIndexTool,
)
