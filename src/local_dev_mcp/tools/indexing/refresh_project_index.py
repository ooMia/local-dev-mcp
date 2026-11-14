"""Explicit structural Trail build for the immutable project root."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ._manager import IndexReport

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class RefreshProjectIndexTool:
    settings: ServerSettings

    async def execute(self) -> IndexReport:
        return await self.settings.index.refresh()


REFRESH_PROJECT_INDEX: Final = ToolDefinition(
    name="refresh_project_index",
    description=(
        "Refresh only the selected instance's Policy.root using Trail build if the structural index is stale. "
        "Serializes concurrent refreshes, verifies with a second check, and never uses --deep. "
        "No workspace path or shell command input is accepted. "
        "Requires explicit 'trail' permission and a working executable; unavailable is not success."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=(),
    create_runtime=RefreshProjectIndexTool,
)
