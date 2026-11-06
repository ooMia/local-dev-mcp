"""Public task preparation and process outcomes, without test-count inference."""

from typing import Literal, TypedDict

from ...settings import JobKind, TestLevel

type TaskState = Literal[
    "running", "succeeded", "failed", "timed_out", "cancelled", "start_failed", "cleanup_unconfirmed"
]


class TaskInfo(TypedDict):
    name: str
    kind: JobKind
    level: TestLevel | None
    permission: str
    ready: bool
    reason: str | None
    next_action: str
    timeout_seconds: int
    default_targets: list[str]


class TaskResult(TypedDict):
    job_id: str
    request_id: str
    task: str
    kind: JobKind
    level: TestLevel | None
    state: TaskState
    exit_code: int | None
    termination_confirmed: bool
    output_included: bool
    stdout: str
    stderr: str
    output_complete: bool
    output_reason: str | None
    started_at: str | None
    finished_at: str | None
    targets: list[str]
    all_tests: bool
    next_action: str
    may_have_changed_files: bool
