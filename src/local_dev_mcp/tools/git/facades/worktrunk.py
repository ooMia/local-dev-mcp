"""Opt-in MCP surface for isolated Worktrunk adoption and validation.

Only creation and cleanup are executable here; the integration proposal is read-only.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._prepare import git_readiness
from ..worktrunk import WorktrunkAdapter
from ..worktrunk.cleanup_plan import cleanup_plan
from ..worktrunk.inventory import inventory
from ..worktrunk.review import review
from ._refs import BranchName, ExpectedHead

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class InspectProjectWorktrunkTool:
    settings: ServerSettings

    async def execute(self, branch: BranchName) -> dict[str, object]:
        return await WorktrunkAdapter(self.settings).inspect(branch)


@dataclass(frozen=True)
class ListProjectWorktreesTool:
    settings: ServerSettings

    async def execute(self, include_branches: bool = True, include_remote: bool = False) -> dict[str, object]:
        return await inventory(self.settings, include_branches=include_branches, include_remote=include_remote)


@dataclass(frozen=True)
class GetProjectWorktreeReviewTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_head: ExpectedHead,
        include_patch: bool = False,
        base_branch: str = "main",
    ) -> dict[str, object]:
        return await review(
            self.settings,
            branch=branch,
            expected_head=expected_head,
            include_patch=include_patch,
            base_branch=base_branch,
        )


@dataclass(frozen=True)
class PlanProjectWorktreeCleanupTool:
    settings: ServerSettings

    async def execute(self, min_age_days: int = 1) -> dict[str, object]:
        return await cleanup_plan(self.settings, min_age_days=min_age_days)


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


PLAN_PROJECT_WORKTREE_CLEANUP: Final = ToolDefinition(
    name="plan_project_worktree_cleanup",
    description=(
        "Read-only Worktrunk prune --dry-run with an exact local main and checked-out branch verification. "
        "Only report clean, accessible, Git-ancestor-integrated candidates; never remove Worktrees or branches. "
        "Unsupported CLI/permissions return an incomplete plan, not a fabricated safe candidate."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=PlanProjectWorktreeCleanupTool,
    readiness=git_readiness,
)


GET_PROJECT_WORKTREE_REVIEW: Final = ToolDefinition(
    name="get_project_worktree_review",
    description=(
        "Review an exact authorized Git-registered Worktrunk checkout: compare committed changes "
        "against a stable local base, count visible staged/unstaged/untracked changes, "
        "and optionally return restricted-path-filtered bounded patches. "
        "No staging, checkout, network access, merging, or untracked content preview."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectWorktreeReviewTool,
    readiness=git_readiness,
)


LIST_PROJECT_WORKTREES: Final = ToolDefinition(
    name="list_project_worktrees",
    description=(
        "Read-only Git inventory of registered local worktrees and optional branch-only rows. "
        "Filter disallowed paths before exposing any checkout, mark unknown Worktrunk facts null, "
        "and enrich only when Worktrunk Schema 2 agrees with Git identity. "
        "include_remote=true is unsupported; no fetch, push or network requests."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=ListProjectWorktreesTool,
    readiness=git_readiness,
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
        "Revalidate the exact branch HEAD, clean checkout and registered path before invoking Worktrunk. "
        "Use Worktrunk's non-force removal and safe integrated-branch deletion rules. "
        "delete_branch=null follows Worktrunk's normal cleanup, false preserves the branch, "
        "true requests its safe deletion. run_hooks=false disables lifecycle hooks; true opts in. "
        "Verify post-removal filesystem and Git state; never trust the CLI receipt alone."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=RemoveProjectWorktrunkTool,
    readiness=git_readiness,
)
