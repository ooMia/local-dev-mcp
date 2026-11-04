"""Stage explicit literal paths, including additions, changes and deletions."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ...registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import add
from . import _repository
from ._repository import PathsResult, ProjectPaths, observed_at

if TYPE_CHECKING:
    from ....runtime import ServerSettings


@dataclass(frozen=True)
class StageProjectPathsTool:
    settings: ServerSettings

    async def execute(self, paths: ProjectPaths) -> PathsResult:
        repo = GitExecution(self.settings)
        checked = _repository.check_paths(repo, paths)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        await add.add_all(repo, checked)
        return {"project": str(repo.root), "success": True, "paths": checked, "observed_at": observed_at()}


STAGE_PROJECT_PATHS: Final = ToolDefinition(
    name="stage_project_paths",
    description=(
        "Stage additions, modifications and deletions at explicit project-relative paths. "
        "'.' stages all; selected partial staging may be replaced."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=StageProjectPathsTool,
    readiness=git_readiness,
)
