"""Typed workspace results and response byte budgets."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .errors import WorkspaceError

RESPONSE_BYTES = 32 * 1024
BODY_BYTES = 16 * 1024
FILE_BYTES = 8 * 1024 * 1024

type RelativePath = Annotated[str, Field(min_length=1, description="Literal workspace-relative path; '.' is the root.")]
type ResultLimit = Annotated[
    int, Field(ge=1, le=200, description="Maximum returned entries; content mode permits 100.")
]
type ListOffset = Annotated[int, Field(ge=0, description="Zero-based offset in the current sorted directory listing.")]
type StartLine = Annotated[int, Field(ge=1, description="One-based first line to read.")]
type LineLimit = Annotated[int, Field(ge=1, le=500, description="Requested complete lines; byte limits also apply.")]
type StopReason = Literal["result_limit", "response_limit", "line_limit", "scan_limit", "deadline", "read_errors"]


def now() -> str:
    return datetime.now(UTC).isoformat()


class FileResult(BaseModel):
    project: str
    complete: bool = True
    reason: StopReason | None = None
    next_action: str | None = None
    observed_at: str = Field(default_factory=now)

    def stop(self, reason: StopReason) -> None:
        self.complete = False
        self.reason = reason
        self.next_action = "Use the next position if provided, or narrow the path, pattern or query and request again."

    def fits(self) -> bool:
        # Leave room for completion metadata and a reference added after reading.
        return len(self.model_dump_json().encode("utf-8")) <= RESPONSE_BYTES - 1024

    def bounded(self) -> None:
        if len(self.model_dump_json().encode("utf-8")) > RESPONSE_BYTES:
            raise WorkspaceError.response()


class Entry(BaseModel):
    path: str
    kind: Literal["file", "directory", "symlink", "other"]


class Listing(FileResult):
    path: str
    entries: list[Entry] = Field(default_factory=list[Entry])
    next_offset: int | None = None


class PathMatches(FileResult):
    path: str
    pattern: str
    paths: list[str] = Field(default_factory=list[str])


class Skipped(BaseModel):
    binary: int = 0
    encoding: int = 0
    too_large: int = 0
    io: int = 0
    special: int = 0


class Match(BaseModel):
    path: str
    line: int
    preview: str
    preview_truncated: bool


class TextMatches(FileResult):
    path: str
    output: Literal["paths", "content"]
    paths: list[str] = Field(default_factory=list[str])
    matches: list[Match] = Field(default_factory=list[Match])
    skipped: Skipped = Field(default_factory=Skipped)


class FileRead(FileResult):
    path: str
    content: str = ""
    start_line: int
    end_line: int | None = None
    total_lines: int = 0
    next_start_line: int | None = None
    version: str = ""
    read_ref: str | None = None


class NextCall(BaseModel):
    tool: str
    arguments: dict[str, object]


class FileEdit(BaseModel):
    project: str
    path: str
    outcome: Literal["applied", "unchanged", "rejected", "applied_unverified"] = "rejected"
    code: str | None = None
    reason: str | None = None
    changed: bool = False
    version_before: str | None = None
    version_after: str | None = None
    read_ref: str | None = None
    content: str | None = None
    start_line: int = 1
    end_line: int | None = None
    next_action: str | None = None
    next_call: NextCall | None = None
    cleanup_warning: str | None = None
    observed_at: str = Field(default_factory=now)
