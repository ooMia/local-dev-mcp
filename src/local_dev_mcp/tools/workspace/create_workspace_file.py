"""Create one new text file without overwriting an existing entry."""

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
class CreateWorkspaceFileTool:
    settings: ServerSettings

    async def execute(
        self,
        path: RelativePath,
        content: Annotated[str, Field(description="Exact UTF-8 content, at most 16 KiB, without NUL. Empty is valid.")],
        request_id: RequestId,
        create_parents: bool = False,
    ) -> FileMutation:
        return await self.settings.files.create_file(path, content, request_id, create_parents)


CREATE_WORKSPACE_FILE: Final = ToolDefinition(
    name="create_workspace_file",
    description=(
        "Create one absent UTF-8 file in the selected workspace; existing entries are never overwritten. "
        "Use create_parents=true explicitly when missing permitted parent directories should be created. "
        "Inspect outcome, code, created_directories and next_call: failures can leave newly created parents. "
        "Use a fresh request_id for new work and the same ID and identical inputs after response loss; "
        "a replay returns the original receipt, not a fresh observation. Verified creation returns a read_ref. "
        "Path permissions, read-only rules and gitignore still apply. No automatic staging or commit."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=CreateWorkspaceFileTool,
    compact_result=True,
)
