"""Read one server-local CLI job without repeating execution."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from mcp.types import ToolAnnotations
from pydantic import Field

from ...mcp.registry import ToolDefinition
from ._response import TaskResult

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

type JobId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


@dataclass(frozen=True)
class GetProjectTaskTool:
    settings: ServerSettings

    async def execute(self, job_id: JobId, include_output: bool = False) -> TaskResult:
        return self.settings.tasks.get(job_id, include_output=include_output)


GET_PROJECT_TASK: Final = ToolDefinition(
    name="get_project_task",
    description=(
        "Read a running or retained CLI job by job_id. Does not restart it. Returns metadata by default; "
        "set include_output=true to read retained masked stdout/stderr. output_included=false means those "
        "strings were omitted, not that the process emitted no output. Output is bounded and redacted; "
        "inspect state, exit_code, termination_confirmed, output_complete and next_action. A successful process exit "
        "alone does not establish test counts or complete regression coverage. Results are server/workspace-local "
        "and are not recoverable after server restart or expiry."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=GetProjectTaskTool,
    compact_result=True,
)
