"""Start a registered recipe with explicit test selection and response-loss recovery."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from mcp.types import ToolAnnotations
from pydantic import Field

from ...mcp.registry import ToolDefinition
from ._response import TaskResult

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

type TaskName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
type RequestId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
type Targets = Annotated[list[Annotated[str, Field(min_length=1, max_length=1024)]], Field(min_length=1, max_length=32)]


@dataclass(frozen=True)
class StartProjectTaskTool:
    settings: ServerSettings

    async def execute(
        self,
        task: TaskName,
        request_id: RequestId,
        targets: Targets | None = None,
        all_tests: bool = False,
    ) -> TaskResult:
        return await self.settings.tasks.start(task, request_id, targets, all_tests)


START_PROJECT_TASK: Final = ToolDefinition(
    name="start_project_task",
    description=(
        "Run one registered CLI recipe; inspect list_project_tasks first. For tests supply explicit relative "
        "targets/node IDs, or all_tests=true to use the configured full targets. "
        "Explicit targets replace full targets; "
        "no automatic scope expansion or arbitrary CLI options. lint/format use their fixed recipe. "
        "Use a new request_id for a new execution and reuse that same ID and inputs after a lost response. "
        "Quick jobs return completion metadata; longer jobs return a job_id for get_project_task/cancel_project_task. "
        "Output is omitted by default; request get_project_task(job_id, include_output=true) for retained diagnostics. "
        "Only one recipe runs per workspace/server. Wait before editing its inputs. All recipes can have side effects; "
        "timeout, cancellation and nonzero exit do not imply unchanged files. Diagnostics mask known private values "
        "and supported encodings, not arbitrary unknown or derived secrets."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    required_commands=(),
    create_runtime=StartProjectTaskTool,
    compact_result=True,
)
