"""Opt-in MCP surface for isolated Worktrunk adoption and validation.

Do not retire the existing Git worktree tools until behavior is compared.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._prepare import git_readiness
from ..worktrunk import WorktrunkAdapter
from ._refs import BranchName, ExpectedHead

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class CreateProjectWorktrunkTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_base_head: ExpectedHead,
        base_branch: str = "main",
    ) -> dict[str, str]:
        return await WorktrunkAdapter(self.settings).create(
            branch, base_branch, expected_base_head, expected_branch, expected_head
        )


@dataclass(frozen=True)
class MergeProjectWorktrunkTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_source_head: ExpectedHead,
        expected_main_head: ExpectedHead,
        mode: Literal["squash", "ff"] = "squash",
    ) -> dict[str, str]:
        return await WorktrunkAdapter(self.settings).merge(branch, expected_source_head, expected_main_head, mode=mode)


@dataclass(frozen=True)
class RemoveProjectWorktrunkTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        delete_branch: bool = False,
    ) -> dict[str, object]:
        return await WorktrunkAdapter(self.settings).remove(branch, expected_branch_head, delete_branch=delete_branch)


CREATE_PROJECT_WORKTRUNK: Final = ToolDefinition(
    name="create_project_worktrunk",
    description=(
        "Create a new topic branch and linked Worktrunk worktree under the authorized .lab/wt "
        "directory, from an exact observed local base. Preserve the original checkout and its "
        "dirty files. Refuse occupied branch/path; never clobber or invoke Worktrunk hooks. "
        "On uncertain failure inspect Git refs and worktrees before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=CreateProjectWorktrunkTool,
    readiness=git_readiness,
)

MERGE_PROJECT_WORKTRUNK: Final = ToolDefinition(
    name="merge_project_worktrunk",
    description=(
        "Merge one clean managed Worktrunk feature into an observed clean local main. "
        "Requires exact source/main SHAs and ancestry. Squash by default or native fast-forward; "
        "disable auto-stage, rebase, Worktrunk hooks and automatic removal. Verify result tree. "
        "Do not replay after timeout, conflict or unverified outcome."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=MergeProjectWorktrunkTool,
    readiness=git_readiness,
)

REMOVE_PROJECT_WORKTRUNK: Final = ToolDefinition(
    name="remove_project_worktrunk",
    description=(
        "Remove only an exact, clean managed Worktrunk worktree in the foreground. "
        "Refuse ignored files and changed source HEAD. Branch deletion is independently "
        "opted in and uses Worktrunk's non-force integration checks. Never force removal."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=RemoveProjectWorktrunkTool,
    readiness=git_readiness,
)
