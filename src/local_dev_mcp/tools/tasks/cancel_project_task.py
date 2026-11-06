"""Cancel one running CLI job and report confirmed process cleanup."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ._response import TaskResult
from .get_project_task import JobId

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


@dataclass(frozen=True)
class CancelProjectTaskTool:
    settings: ServerSettings

    async def execute(self, job_id: JobId) -> TaskResult:
        return await self.settings.tasks.cancel(job_id)


CANCEL_PROJECT_TASK: Final = ToolDefinition(
    name="cancel_project_task",
    description=(
        "Request cancellation of a CLI job and wait for direct process cleanup. Returns metadata with output omitted; "
        "use get_project_task with include_output=true to inspect retained masked diagnostics. "
        "A completed job keeps its original "
        "result. Inspect termination_confirmed; cancellation does not roll back format or other filesystem effects "
        "and does not establish that every indirect descendant process has exited. Read current files before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=(),
    create_runtime=CancelProjectTaskTool,
    compact_result=True,
)
