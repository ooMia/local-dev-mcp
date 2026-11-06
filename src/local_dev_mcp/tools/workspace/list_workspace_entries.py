"""One directory page, independent of Git and commit preparation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ._response import Listing, ListOffset, RelativePath, ResultLimit

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class ListWorkspaceEntriesTool:
    settings: ServerSettings

    async def execute(self, path: RelativePath = ".", offset: ListOffset = 0, limit: ResultLimit = 100) -> Listing:
        return await self.settings.files.list_entries(path, offset, limit)


LIST_WORKSPACE_ENTRIES: Final = ToolDefinition(
    name="list_workspace_entries",
    description=(
        "List one workspace directory in name order. Entries include file/directory/symlink/other kinds. "
        "Protected and ignored names are omitted. Use next_offset for another current-directory page; "
        "concurrent directory changes can shift pages. Inspect complete/reason before treating results as exhaustive."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=ListWorkspaceEntriesTool,
    compact_result=True,
)
