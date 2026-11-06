"""Own server/workspace-bound retained reads and their bounded lifetime."""

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from ._access import ScopedPath, Workspace
from ._response import FileRead
from .errors import WorkspaceError

REFERENCE_COUNT = 256
REFERENCE_BYTES = 8 * 1024 * 1024
REFERENCE_SECONDS = 30 * 60


@dataclass(frozen=True)
class StoredRead:
    path: ScopedPath
    content: str
    version: str
    start_line: int
    end_line: int | None
    issued: float


class ReadStore:
    workspace: Workspace
    clock: Callable[[], float]
    _items: OrderedDict[str, StoredRead]
    _bytes: int
    _lock: threading.Lock

    def __init__(self, workspace: Workspace, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.workspace = workspace
        self.clock = clock
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def _remove_first(self) -> None:
        _, record = self._items.popitem(last=False)
        self._bytes -= len(record.content.encode("utf-8"))

    def _expire(self) -> None:
        while self._items and self.clock() - next(iter(self._items.values())).issued >= REFERENCE_SECONDS:
            self._remove_first()

    def put(self, path: ScopedPath, result: FileRead) -> str:
        path.require_workspace(self.workspace)
        path.checked()
        if (
            (not result.content and not (result.total_lines == 0 and result.start_line == 1))
            or (result.content and result.end_line is None)
            or result.path != path.relative
            or result.project != str(self.workspace.policy.root)
        ):
            raise WorkspaceError.reference()
        record = StoredRead(path, result.content, result.version, result.start_line, result.end_line, self.clock())
        size = len(record.content.encode("utf-8"))
        if size > REFERENCE_BYTES:
            raise WorkspaceError.reference()
        with self._lock:
            self._expire()
            while self._items and (len(self._items) >= REFERENCE_COUNT or self._bytes + size > REFERENCE_BYTES):
                self._remove_first()
            token = secrets.token_urlsafe(32)
            self._items[token] = record
            self._bytes += size
        return token

    def get(self, token: str, path: ScopedPath) -> StoredRead:
        path.require_workspace(self.workspace)
        path.checked()
        with self._lock:
            self._expire()
            record = self._items.get(token)
            if record is None or record.path != path:
                raise WorkspaceError.reference()
            return record

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0
