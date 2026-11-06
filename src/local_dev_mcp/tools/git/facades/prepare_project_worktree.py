"""Prepare an existing topic branch without switching the original checkout."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ....settings import protected_name
from .. import _worktrees
from .._execution import GitExecution, text_output
from .._prepare import git_readiness
from .._remote import feature_name
from ..commands import status, worktree
from ._refs import BranchName, ExpectedHead, resolve_local_ref

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class PrepareProjectWorktreeTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
    ) -> _worktrees.WorktreeState:
        _worktrees.require_permission(self.settings, write=True)
        feature_name(branch)
        fingerprint = await _worktrees.original_snapshot(self.settings, expected_branch, expected_head)
        repo = GitExecution(self.settings)
        _, head = await resolve_local_ref(repo, branch)
        if head != expected_branch_head:
            raise ToolError("Target branch changed since observation.")
        path, receipt = _worktrees.locations(self.settings, branch, write=True)
        records = await worktree.list_registered(repo)
        if (
            path.exists()
            or receipt.exists()
            or any(row.path == path or row.branch == "refs/heads/" + branch for row in records)
        ):
            raise ToolError("Target path, receipt or branch is already occupied; inspect it before continuing.")
        await worktree.require_ignored(repo, path.relative_to(repo.root).as_posix() + "/")
        await worktree.require_ignored(repo, receipt.relative_to(repo.root).as_posix())
        # Checkout must not materialize a tracked secret or escape an explicit path policy.
        tree = await repo.run("ls-tree", "-r", "--name-only", "-z", head, operation="Check checkout paths")
        for name in filter(None, text_output(tree.output).split("\0")):
            if any(protected_name(part) for part in name.split("/")):
                raise ToolError("Target tree contains protected paths; no worktree was created.")
            candidate = path / name
            if not self.settings.policy.allows_path(candidate) or not self.settings.policy.allows_path(
                repo.root / name
            ):
                raise ToolError("Target checkout includes paths outside the configured access policy.")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _worktrees.locations(self.settings, branch, write=True)
            _worktrees.write_receipt(receipt, branch, head, prepared=False)
            if (await resolve_local_ref(repo, branch))[1] != head:
                raise ToolError("Target branch moved before checkout; retained preparation is partial.")
            await worktree.add_existing_branch(repo, path, branch)
            target = _worktrees.scoped_settings(self.settings, path)
            actual_branch, actual_head, entries = await status.read_porcelain_v2(GitExecution(target))
            if actual_branch != branch or actual_head != head or entries:
                raise ToolError("Worktree checkout was not clean at the expected SHA; inspect retained state.")
            _worktrees.write_receipt(receipt, branch, head, prepared=True)
        finally:
            await _worktrees.confirm_original(self.settings, expected_branch, expected_head, fingerprint)
        result = await _worktrees.inspect(self.settings, branch)
        if result["state"] != "ready" or result["clean"] is not True or result["head"] != head:
            raise ToolError("Worktree may have been created but verification failed; inspect before retrying.")
        return result


PREPARE_PROJECT_WORKTREE: Final = ToolDefinition(
    name="prepare_project_worktree",
    description=(
        "Prepare an observed existing topic branch in a fixed ignored linked worktree without switching the original. "
        "Requires exact original and target SHAs and git.worktree.write. Never overwrites paths or copies credentials. "
        "Failure may leave partial files/registration; inspect_project_worktree before any retry."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "git.worktree.write"),
    create_runtime=PrepareProjectWorktreeTool,
    readiness=git_readiness,
)
