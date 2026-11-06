"""Structured porcelain-v2 status for the configured project."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import status
from ..commands.status import StatusEntry
from . import _repository, _visibility
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class ProjectStatus(TypedDict):
    project: str
    branch: str | None
    head: str | None
    entries: list[StatusEntry]
    complete: bool
    reason: str | None
    next_action: str | None
    observed_at: str


@dataclass(frozen=True)
class GetProjectStatusTool:
    settings: ServerSettings

    async def execute(self) -> ProjectStatus:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        branch, head, entries = await status.read_porcelain_v2(repo)
        visible = [
            entry
            for entry in entries
            if _visibility.readable(repo, entry["path"])
            and (entry["original_path"] is None or _visibility.readable(repo, entry["original_path"]))
        ]
        excluded = len(visible) != len(entries)
        return {
            "project": str(repo.root),
            "branch": branch,
            "head": head,
            "entries": visible,
            "complete": not excluded,
            "reason": "restricted_paths" if excluded else None,
            "next_action": _visibility.restricted_message() if excluded else None,
            "observed_at": observed_at(),
        }


GET_PROJECT_STATUS: Final = ToolDefinition(
    name="get_project_status",
    description=(
        "Read local branch, HEAD and staged, unstaged or untracked project paths without changing files. "
        "Unreadable paths are omitted with complete=false; an empty filtered result is not a clean repository."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectStatusTool,
    readiness=git_readiness,
)
