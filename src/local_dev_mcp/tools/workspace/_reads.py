"""Bounded observations: stream raw bytes once and retain only returned text."""

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, Literal

from ._access import ScopedPath
from ._response import BODY_BYTES, FILE_BYTES, FileRead, StopReason
from ._text import TextDataError, TextLines
from .errors import WorkspaceError

SCAN_ENTRIES = 10_000
SCAN_BYTES = 32 * 1024 * 1024
SCAN_SECONDS = 10


def no_cancel() -> None:
    pass


class ScanStopped(Exception):
    reason: StopReason

    def __init__(self, reason: StopReason) -> None:
        self.reason = reason


class SkippedFile(Exception):
    kind: Literal["binary", "encoding", "too_large"]

    def __init__(self, kind: Literal["binary", "encoding", "too_large"]) -> None:
        self.kind = kind


@dataclass
class Budget:
    cancel: Callable[[], None] = no_cancel
    started: float = field(default_factory=time.monotonic)
    entries: int = 0
    read_bytes: int = 0

    def check(self) -> None:
        self.cancel()
        if time.monotonic() - self.started >= SCAN_SECONDS:
            raise ScanStopped("deadline")

    def entry(self) -> None:
        self.check()
        if self.entries >= SCAN_ENTRIES:
            raise ScanStopped("scan_limit")
        self.entries += 1

    def consume(self, count: int) -> None:
        self.check()
        self.read_bytes += count
        if self.read_bytes > SCAN_BYTES:
            raise ScanStopped("scan_limit")


def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass
class TextObservation:
    path: ScopedPath
    budget: Budget
    policy_metadata: bool = False
    version: str = ""
    total_lines: int = 0

    def _chunks(self, stream: BinaryIO, size: int) -> Iterator[bytes]:
        count = 0
        while count < size:
            self.budget.check()
            remaining = SCAN_BYTES - self.budget.read_bytes
            if remaining <= 0:
                raise ScanStopped("scan_limit")
            chunk = stream.read(min(65536, remaining, size - count))
            if not chunk:
                break
            self.budget.consume(len(chunk))
            count += len(chunk)
            yield chunk

    def lines(self) -> Iterator[str]:
        descriptor, before = self.path.open_binary(policy_metadata=self.policy_metadata)
        try:
            with os.fdopen(descriptor, "rb") as stream:
                if before.st_size > FILE_BYTES:
                    raise SkippedFile("too_large")
                text = TextLines(FILE_BYTES)
                for line in text.lines(self._chunks(stream, before.st_size)):
                    self.budget.check()
                    self.total_lines = text.total_lines
                    yield line
                after = os.fstat(stream.fileno())
                if identity(before) != identity(after) or identity(after) != identity(
                    self.path.checked(policy_metadata=self.policy_metadata).stat()
                ):
                    raise WorkspaceError.changed()
                self.version = text.version
        except TextDataError as error:
            raise SkippedFile(error.kind) from None
        except OSError:
            raise WorkspaceError.access() from None


def read_file(path: ScopedPath, start_line: int, max_lines: int, budget: Budget) -> FileRead:
    result = FileRead(project=str(path.workspace.policy.root), path=path.relative, start_line=start_line)
    observation = TextObservation(path, budget)
    size = 0
    selected = 0
    stopped = False
    try:
        for number, line in enumerate(observation.lines(), 1):
            if number < start_line:
                continue
            if stopped:
                continue
            if selected >= max_lines:
                result.stop("line_limit")
                result.next_start_line = number
                stopped = True
                continue
            previous = result.content
            result.content += line
            line_size = len(line.encode("utf-8"))
            if size + line_size > BODY_BYTES or not result.fits():
                result.content = previous
                if selected == 0:
                    raise WorkspaceError.line()
                result.stop("response_limit")
                result.next_start_line = number
                stopped = True
                continue
            size += line_size
            selected += 1
            result.end_line = number
    except SkippedFile as error:
        if error.kind == "too_large":
            raise WorkspaceError.size() from None
        raise WorkspaceError.text() from None
    except ScanStopped:
        raise WorkspaceError.budget() from None
    result.version = observation.version
    result.total_lines = observation.total_lines
    if start_line > result.total_lines and result.total_lines:
        result.next_action = f"The requested start is past EOF. Choose start_line within 1..{result.total_lines}."
    result.bounded()
    return result
