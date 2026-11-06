"""Read exact text ranges and issue bounded server-local observations."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ._response import FileRead, LineLimit, RelativePath, StartLine

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class ReadWorkspaceFileTool:
    settings: ServerSettings

    async def execute(self, path: RelativePath, start_line: StartLine = 1, max_lines: LineLimit = 200) -> FileRead:
        return await self.settings.files.read_file(path, start_line, max_lines)


READ_WORKSPACE_FILE: Final = ToolDefinition(
    name="read_workspace_file",
    description=(
        "Read complete UTF-8 lines without whitespace/newline normalization. Whole-file version identifies raw "
        "bytes, not a Git object. Files over 8 MiB or containing NUL are rejected. Use next_start_line for more. "
        "Observed ranges and empty files receive a read_ref for up to 30 minutes, subject to eviction/restart. "
        "References are server/workspace-local, not Chat-authenticated. Use replace_workspace_file to edit that range. "
        "Protected paths and symlinks are rejected; ignored regular files can be read directly."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=ReadWorkspaceFileTool,
    compact_result=True,
)
