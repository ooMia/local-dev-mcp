"""Observe HEAD, Git index fingerprint and changed paths before selective cherry-picks."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import revisions, status
from . import _repository, _visibility
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class ProjectPickState(TypedDict):
    project: str
    branch: str | None
    head: str | None
    index_fingerprint: str
    staged_paths: list[str]
    unstaged_paths: list[str]
    untracked_paths: list[str]
    complete: bool
    observed_at: str


@dataclass(frozen=True)
class GetProjectPickStateTool:
    settings: ServerSettings

    async def execute(self) -> ProjectPickState:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        branch, head, entries = await status.read_porcelain_v2(repo)
        fingerprint = await revisions.index_fingerprint(repo)
        visible = [e for e in entries if _visibility.readable(repo, e["path"])]
        return {
            "project": str(repo.root),
            "branch": branch,
            "head": head,
            "index_fingerprint": fingerprint,
            "staged_paths": [e["path"] for e in visible if e["index_status"] not in (".", "?")],
            "unstaged_paths": [e["path"] for e in visible if e["worktree_status"] not in (".", "?")],
            "untracked_paths": [e["path"] for e in visible if e["kind"] == "untracked"],
            "complete": len(visible) == len(entries),
            "observed_at": observed_at(),
        }


GET_PROJECT_PICK_STATE: Final = ToolDefinition(
    name="get_project_pick_state",
    description=(
        "Observe branch/HEAD, SHA-256 fingerprint of staged index records, and visible paths. "
        "Use this fingerprint as a precondition for a selective cherry-pick or removal."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectPickStateTool,
    readiness=git_readiness,
)
