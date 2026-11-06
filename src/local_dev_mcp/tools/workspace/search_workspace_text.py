"""Literal search with compact path output or bounded line previews."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ...mcp.registry import ToolDefinition
from ._response import RelativePath, ResultLimit, TextMatches

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

type LiteralQuery = Annotated[
    str, Field(min_length=1, description="Nonempty single-line literal text, not a regular expression; NUL forbidden.")
]


@dataclass(frozen=True)
class SearchWorkspaceTextTool:
    settings: ServerSettings

    async def execute(
        self,
        query: LiteralQuery,
        path: RelativePath = ".",
        output: Literal["paths", "content"] = "paths",
        case_sensitive: bool = True,
        limit: ResultLimit = 100,
    ) -> TextMatches:
        return await self.settings.files.search_text(query, path, output, case_sensitive, limit)


SEARCH_WORKSPACE_TEXT: Final = ToolDefinition(
    name="search_workspace_text",
    description=(
        "Recursively search UTF-8 regular files for literal text. Default paths output returns each matching path "
        "once. Content output returns at most 100 matching lines, with 512-byte previews and truncation flags. "
        "Previews are not edit references: read the required range before editing. Inspect complete and skipped "
        "counts; incomplete zero-match results do not establish absence. Narrow the scope if limits are reached."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=SearchWorkspaceTextTool,
    compact_result=True,
)
