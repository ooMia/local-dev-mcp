"""Read-only MCP projection of the registered repository's document snapshot."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from anyio import to_thread
from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from .capture import diagnose_repository
from .diagnostic import DiagnosticResult

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class DiagnoseDocumentsTool:
    settings: ServerSettings

    async def execute(self, repository_id: str) -> DiagnosticResult:
        return await to_thread.run_sync(lambda: diagnose_repository(self.settings.policy, repository_id))


DIAGNOSE_DOCUMENTS: Final = ToolDefinition(
    name="diagnose_documents",
    description=(
        "Read and diagnose the registered local-dev-mcp Working Tree's five managed documents. "
        "Requires explicit repository_id; never accepts arbitrary paths, creates approvals or edits files. "
        "Pending relationship approval returns completion=Blocked with document outcomes unset; "
        "execution and snapshot changes are tool errors."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=DiagnoseDocumentsTool,
    compact_result=True,
)
