"""Replace one observed range of an existing file, reporting exact write and recovery state."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from mcp.types import ToolAnnotations
from pydantic import Field

from ...mcp.registry import ToolDefinition
from ._response import FileEdit, RelativePath

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class ReplaceWorkspaceFileTool:
    settings: ServerSettings

    async def execute(
        self,
        path: RelativePath,
        read_ref: Annotated[str, Field(min_length=1, description="A valid observation for this file and server.")],
        replacement: Annotated[
            str, Field(description="Exact replacement for the whole observed range; UTF-8 <=16 KiB.")
        ],
    ) -> FileEdit:
        return await self.settings.files.replace_file(path, read_ref, replacement)


REPLACE_WORKSPACE_FILE: Final = ToolDefinition(
    name="replace_workspace_file",
    description=(
        "Replace the complete range of one read_ref in an existing UTF-8 file. Do not send the whole file unless "
        "that is the observed range. Whole-file version conflicts preserve the current file: read again, locate "
        "the intended text and reconstruct the edit. No automatic merge or retry. Empty existing files are supported. "
        "Inspect outcome, changed, code and next_call. applied_unverified means this request replaced the file but "
        "could not finish read-back; read before retrying. Verified results provide content and a new read_ref. "
        "No newline normalization, auto-stage or commit. Read-only, ignored, protected and denied paths reject edits."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=(),
    create_runtime=ReplaceWorkspaceFileTool,
    compact_result=True,
)
