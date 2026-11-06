"""Run registered checks in a pinned managed worktree through the shared task manager."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ....settings import Policy
from ...tasks import RequestId, Targets, TaskName, TaskResult, TaskWorktree
from .. import _worktrees
from .._prepare import git_readiness
from .._remote import feature_name
from ._refs import BranchName, ExpectedHead

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class StartProjectWorktreeTaskTool:
    settings: ServerSettings

    async def execute(
        self,
        task: TaskName,
        request_id: RequestId,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        targets: Targets | None = None,
        all_tests: bool = False,
    ) -> TaskResult:
        _worktrees.require_permission(self.settings, write=True)
        feature_name(branch)
        fingerprint: str | None = None

        async def prepare() -> Policy:
            nonlocal fingerprint
            observed = await _worktrees.original_snapshot(self.settings, expected_branch, expected_head)
            if fingerprint is not None and fingerprint != observed:
                raise ToolError("Original index changed before the worktree check started.")
            fingerprint = observed
            target = await _worktrees.require_ready(self.settings, branch, expected_branch_head)
            _worktrees.locations(self.settings, branch, write=True)
            return target.policy

        async def verify() -> None:
            try:
                await _worktrees.require_ready(self.settings, branch, expected_branch_head)
            finally:
                if fingerprint is None:
                    raise ToolError("Original state was not captured; inspect both worktrees.")
                await _worktrees.confirm_original(self.settings, expected_branch, expected_head, fingerprint)

        context = TaskWorktree(branch, expected_branch_head, expected_branch, expected_head, prepare, verify)
        return await self.settings.tasks.start(task, request_id, targets, all_tests, worktree=context)


START_PROJECT_WORKTREE_TASK: Final = ToolDefinition(
    name="start_project_worktree_task",
    description=(
        "Run one startup-registered lint/test check in an exact prepared managed worktree. "
        "Requires observed original/target refs, git.worktree.write and the selected recipe permission. "
        "Shares the original task manager's single slot, request ledger, get/cancel tools and masked diagnostics. "
        "Never loads historical config or accepts cwd/commands; format recipes are rejected. "
        "Inspect optional worktree.verified independently of process exit and test output. "
        "Recipes may change files; no rollback, automatic retry, commit or publication."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    required_commands=("git", "git.worktree.write"),
    create_runtime=StartProjectWorktreeTaskTool,
    readiness=git_readiness,
    compact_result=True,
)
