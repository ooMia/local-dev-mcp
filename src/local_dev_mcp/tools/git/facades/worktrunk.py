"""Opt-in MCP surface for isolated Worktrunk adoption and validation.

Only creation and cleanup are executable here; the integration proposal is read-only.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._prepare import git_readiness
from ..worktrunk import WorktrunkAdapter
from ._refs import BranchName, ExpectedHead

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class InspectProjectWorktrunkTool:
    settings: ServerSettings

    async def execute(self, branch: BranchName) -> dict[str, object]:
        return await WorktrunkAdapter(self.settings).inspect(branch)


@dataclass(frozen=True)
class CreateProjectWorktrunkTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_base_head: ExpectedHead | None = None,
        base_branch: str = "main",
        run_hooks: bool = False,
        expected_branch_head: ExpectedHead | None = None,
        worktree_path: str | None = None,
    ) -> dict[str, str]:
        return await WorktrunkAdapter(self.settings).create(
            branch,
            base_branch,
            expected_base_head,
            expected_branch,
            expected_head,
            run_hooks=run_hooks,
            expected_branch_head=expected_branch_head,
            worktree_path=worktree_path,
        )


@dataclass(frozen=True)
class RemoveProjectWorktrunkTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        delete_branch: bool | None = None,
        run_hooks: bool = False,
    ) -> dict[str, object]:
        async with self.settings.tasks.exclusive_git_cleanup():
            return await WorktrunkAdapter(self.settings).remove(
                branch, expected_branch_head, delete_branch=delete_branch, run_hooks=run_hooks
            )


INSPECT_PROJECT_WORKTRUNK: Final = ToolDefinition(
    name="inspect_project_worktrunk",
    description=(
        "Read Git-registered Worktrunk checkout state for a topic branch without running wt. "
        "Report absent, not_checked_out, ready, dirty or blocked, with exact branch/head "
        "and an authorized relative checkout path when safe. "
        "Never expose unauthorized paths or require Native preparation receipts. "
        "This observation does not authorize mutation or certify test quality."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=InspectProjectWorktrunkTool,
    readiness=git_readiness,
)


CREATE_PROJECT_WORKTRUNK: Final = ToolDefinition(
    name="create_project_worktrunk",
    description=(
        "Ensure a topic branch has an authorized linked Worktree: reuse an existing registered "
        "checkout or switch an existing branch into an absent allowed path. Create an absent branch "
        "only with expected_base_head. Optionally specify worktree_path (workspace-relative, ignored) "
        "or pin an existing expected_branch_head. "
        "Return action=created/checked_out/reused; preserve dirty files and the original checkout. "
        "run_hooks=false disables lifecycle hooks; true opts in for executed switches. "
        "Never clobber; reobserve uncertain failures before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=CreateProjectWorktrunkTool,
    readiness=git_readiness,
)

REMOVE_PROJECT_WORKTRUNK: Final = ToolDefinition(
    name="remove_project_worktrunk",
    description=(
        "Remove an explicitly identified, registered linked Worktree inside the allowed workspace. "
        "Delegate dirty-file protection and integrated-branch deletion to Worktrunk (non-force). "
        "delete_branch=null follows Worktrunk's normal cleanup, false preserves the branch, "
        "true requests its safe deletion. run_hooks=false disables lifecycle hooks; true opts in. "
        "Check branch_outcome for retained refs; never assume removal also deleted a branch."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=RemoveProjectWorktrunkTool,
    readiness=git_readiness,
)
