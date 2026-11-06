"""Prepare one explicitly reviewed assessment without staging or committing."""

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .. import _worktree_assessments as assessments
from .. import _worktrees
from .._execution import GitExecution
from .._prepare import git_readiness
from ._refs import BranchName, ExpectedHead
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class PreparedWorktreeAssessment(TypedDict):
    project: str
    branch: str
    source_head: str
    path: str
    digest: str
    observed_at: str


@dataclass(frozen=True)
class PrepareProjectWorktreeAssessmentTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        assessment_id: assessments.AssessmentId,
        assessment_toml: assessments.AssessmentToml,
    ) -> PreparedWorktreeAssessment:
        _worktrees.require_permission(self.settings, write=True)
        content = assessment_toml.encode("utf-8")
        record = assessments.parse(content)
        fingerprint = await _worktrees.original_snapshot(self.settings, expected_branch, expected_head)
        try:
            target = await _worktrees.require_ready(self.settings, branch, expected_branch_head)
            path = assessments.selected_path(self.settings, target, assessment_id)
            await assessments.require_new(GitExecution(target), expected_branch_head, assessment_id, record)
            if path.exists():
                raise ToolError("Assessment path already exists; it will not be overwritten.")
            await _worktrees.require_ready(self.settings, branch, expected_branch_head)
            path = assessments.selected_path(self.settings, target, assessment_id)
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as stream:
                stream.write(content)
            assessments.reviewed_bytes(path, hashlib.sha256(content).hexdigest())
            state = await _worktrees.inspect(self.settings, branch)
            if state["state"] != "ready" or state["head"] != expected_branch_head:
                raise ToolError("Assessment may have been written but the target changed; inspect before retrying.")
            return {
                "project": str(self.settings.policy.root),
                "branch": branch,
                "source_head": expected_branch_head,
                "path": path.relative_to(self.settings.policy.root).as_posix(),
                "digest": hashlib.sha256(content).hexdigest(),
                "observed_at": observed_at(),
            }
        finally:
            await _worktrees.confirm_original(self.settings, expected_branch, expected_head, fingerprint)


PREPARE_PROJECT_WORKTREE_ASSESSMENT: Final = ToolDefinition(
    name="prepare_project_worktree_assessment",
    description=(
        "Write one absent .changes assessment in a prepared managed worktree from explicit reviewed TOML. "
        "Return exact-byte SHA-256 digest; never stage, commit, overwrite or infer compatibility. "
        "Requires exact source/target refs and git.worktree.write. Reject duplicate judgements and corrections. "
        "After failure inspect worktree/file state before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "git.worktree.write"),
    create_runtime=PrepareProjectWorktreeAssessmentTool,
    readiness=git_readiness,
)
