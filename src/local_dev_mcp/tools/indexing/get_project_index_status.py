"""Inspect the configured project's Trail index without refreshing it."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ._manager import IndexReport

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class GetProjectIndexStatusTool:
    settings: ServerSettings

    async def execute(self) -> IndexReport:
        return await self.settings.index.status()


GET_PROJECT_INDEX_STATUS: Final = ToolDefinition(
    name="get_project_index_status",
    description=(
        "Read-only Trail structural and deep index status for this instance's Policy.root. "
        "Never selects another workspace or updates the index. "
        "Unavailable when the optional trail command is not permitted or installed."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=GetProjectIndexStatusTool,
)
