"""Stage explicit literal paths, including additions, changes and deletions."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import add, ls_files
from . import _repository, _visibility
from ._repository import PathsResult, ProjectPaths, observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class StageProjectPathsTool:
    settings: ServerSettings

    async def execute(self, paths: ProjectPaths) -> PathsResult:
        repo = GitExecution(self.settings)
        checked = _repository.check_paths(repo, paths)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        _visibility.require_selection(repo, checked)
        candidates = await ls_files.stage_candidates(repo, checked)
        if any(not _visibility.readable(repo, path) for path in candidates):
            raise ToolError(
                "Staging scope includes protected or unreadable paths. No staging was attempted. "
                "Choose explicit allowed paths instead of a wider directory."
            )
        await add.add_all(repo, checked)
        return {"project": str(repo.root), "success": True, "paths": checked, "observed_at": observed_at()}


STAGE_PROJECT_PATHS: Final = ToolDefinition(
    name="stage_project_paths",
    description=(
        "Stage additions, modifications and deletions at explicit project-relative paths. "
        "'.' stages all; selected partial staging may be replaced. "
        "A scope containing unreadable paths is rejected before staging; choose narrower allowed paths."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=StageProjectPathsTool,
    readiness=git_readiness,
)
