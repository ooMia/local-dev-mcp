"""Delete one observed regular text file with whole-file conflict checks."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from mcp.types import ToolAnnotations
from pydantic import Field

from ...mcp.registry import ToolDefinition
from ._mutation_response import FileMutation, RequestId
from ._response import RelativePath

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class DeleteWorkspaceFileTool:
    settings: ServerSettings

    async def execute(
        self,
        path: RelativePath,
        read_ref: Annotated[str, Field(min_length=1, description="A current read_ref bound to this file and server.")],
        request_id: RequestId,
    ) -> FileMutation:
        return await self.settings.files.delete_file(path, read_ref, request_id)


DELETE_WORKSPACE_FILE: Final = ToolDefinition(
    name="delete_workspace_file",
    description=(
        "Permanently delete one existing regular UTF-8 file whose full version still matches read_ref. "
        "The whole file is removed, even if the reference observed only a range. "
        "Directories and symlinks are rejected. "
        "There is no trash, backup or automatic Git restore/stage/commit; unrecorded content may be unrecoverable. "
        "Use the same request_id and identical inputs to recover a lost response without deleting a recreated file. "
        "A replay is the old receipt, not proof of current absence. Inspect outcome/code/next_call before continuing. "
        "Existing permissions, read-only and ignore checks remain in effect."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=(),
    create_runtime=DeleteWorkspaceFileTool,
    compact_result=True,
)
