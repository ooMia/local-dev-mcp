"""Bounded, immutable local commit history for safe feature-by-feature PR reconstruction."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import log, revisions
from ..commands.log import HistoryCommit
from . import _repository
from ._refs import ExpectedHead
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

MAX_COMMITS: Final = 64


class ProjectCommitHistory(TypedDict):
    project: str
    base_ref: str
    target_ref: str
    base_head: str
    target_head: str
    commits: list[HistoryCommit]
    count: int
    observed_at: str


@dataclass(frozen=True)
class InspectProjectCommitHistoryTool:
    settings: ServerSettings

    async def execute(
        self,
        base_ref: str,
        target_ref: str,
        expected_base_head: ExpectedHead,
        expected_target_head: ExpectedHead,
    ) -> ProjectCommitHistory:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        base = await revisions.resolve(repo, base_ref)
        target = await revisions.resolve(repo, target_ref)
        if base != expected_base_head or target != expected_target_head:
            raise ToolError("Selected refs moved; observe full commit SHAs again.")
        common, base_only, count = await revisions.ancestry(repo, base, target)
        if common != base or base_only:
            raise ToolError("The base must be an ancestor of the target before listing publication history.")
        if count > MAX_COMMITS:
            raise ToolError(f"History contains over {MAX_COMMITS} commits. Narrow the reference range.")
        commits = await log.history(repo, base, target, count=count)
        # A concurrent branch update must not yield an apparently current snapshot.
        if await revisions.resolve(repo, base_ref) != base or await revisions.resolve(repo, target_ref) != target:
            raise ToolError("A selected ref moved during history enumeration; inspect it again.")
        return {
            "project": str(repo.root),
            "base_ref": base_ref,
            "target_ref": target_ref,
            "base_head": base,
            "target_head": target,
            "commits": commits,
            "count": count,
            "observed_at": observed_at(),
        }


INSPECT_PROJECT_COMMIT_HISTORY: Final = ToolDefinition(
    name="inspect_project_commit_history",
    description=(
        "List up to 64 locally available commits by full SHA, parent SHAs and concise title between "
        "an exact observed ancestor and a local branch/commit target. Refuses divergent or moved refs; "
        "read-only and does not expose commit body, author credentials, patch or file contents. "
        "Use it to plan source-preserving feature-scoped PR reconstruction, not as an approval to rewrite history."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=InspectProjectCommitHistoryTool,
    readiness=git_readiness,
)
