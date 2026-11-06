"""Unstage explicit paths while preserving their working-tree files."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import restore, rm
from . import _repository
from ._repository import PathsResult, ProjectPaths, observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class UnstageProjectPathsTool:
    settings: ServerSettings

    async def execute(self, paths: ProjectPaths) -> PathsResult:
        repo = GitExecution(self.settings)
        checked = _repository.check_paths(repo, paths)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        if await _repository.read_head(repo) is None:
            await rm.remove_cached_recursive(repo, checked)
        else:
            await restore.restore_staged(repo, checked)
        return {"project": str(repo.root), "success": True, "paths": checked, "observed_at": observed_at()}


UNSTAGE_PROJECT_PATHS: Final = ToolDefinition(
    name="unstage_project_paths",
    description=(
        "Remove selected changes from the index while keeping working files. "
        "Unborn repositories use non-forced cached removal."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=UnstageProjectPathsTool,
    readiness=git_readiness,
)
