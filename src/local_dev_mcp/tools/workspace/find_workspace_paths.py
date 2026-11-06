"""Discover matching regular-file paths without returning their contents."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from mcp.types import ToolAnnotations
from pydantic import Field

from ...mcp.registry import ToolDefinition
from ._response import PathMatches, RelativePath, ResultLimit

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

type PathPattern = Annotated[
    str, Field(min_length=1, description="Relative glob: *, ?, [], **. Example '**/*.py'; no brace or shell expansion.")
]


@dataclass(frozen=True)
class FindWorkspacePathsTool:
    settings: ServerSettings

    async def execute(self, pattern: PathPattern, path: RelativePath = ".", limit: ResultLimit = 100) -> PathMatches:
        return await self.settings.files.find_paths(pattern, path, limit)


FIND_WORKSPACE_PATHS: Final = ToolDefinition(
    name="find_workspace_paths",
    description=(
        "Find regular-file paths using a case-sensitive glob relative to path. No symlinks or protected/ignored "
        "paths are traversed. Returns paths only. On incomplete results narrow path/pattern; no search cursor or total."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=FindWorkspacePathsTool,
    compact_result=True,
)
