"""Serialize observed edits, prepare complete files and publish one atomic replacement."""

import hashlib
import os
import stat
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ._access import ScopedPath, Workspace
from ._discovery import initial_rules
from ._edits import EditRejected, following_lines, replace_span, replacement_bytes
from ._read_refs import ReadStore, StoredRead
from ._reads import Budget, ScanStopped, identity, read_file
from ._response import BODY_BYTES, FILE_BYTES, RESPONSE_BYTES, FileEdit, FileRead, NextCall
from .errors import WorkspaceError


@dataclass
class _LockEntry:
    lock: threading.Lock
    users: int = 0


class WriteLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _LockEntry] = {}

    @contextmanager
    def hold(self, name: str, budget: Budget) -> Generator[None, None, None]:
        with self._guard:
            entry = self._entries.setdefault(name, _LockEntry(threading.Lock()))
            entry.users += 1
        acquired = False
        try:
            while not acquired:
                budget.check()
                acquired = entry.lock.acquire(timeout=0.1)
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._guard:
                entry.users -= 1
                if not entry.users:
                    del self._entries[name]


@dataclass(frozen=True)
class FileSnapshot:
    data: bytes
    info: os.stat_result
    version: str


def snapshot_file(path: ScopedPath, budget: Budget) -> FileSnapshot:
    descriptor, before = path.open_binary()
    with os.fdopen(descriptor, "rb") as stream:
        if before.st_size > FILE_BYTES:
            raise EditRejected("file_too_large", "The current file exceeds 8 MiB. Choose a smaller text file.")
        chunks: list[bytes] = []
        count = 0
        while True:
            budget.check()
            chunk = stream.read(min(65536, FILE_BYTES + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > FILE_BYTES:
                raise EditRejected("file_too_large", "The file grew beyond the 8 MiB file limit.")
            budget.consume(len(chunk))
            chunks.append(chunk)
        after = os.fstat(stream.fileno())
        if identity(before) != identity(after) or identity(after) != identity(path.checked().stat()):
            raise EditRejected("stale_version", "The file changed while being observed. Read it again.")
    data = b"".join(chunks)
    return FileSnapshot(data, after, hashlib.sha256(data).hexdigest())


def require_writable(path: ScopedPath, budget: Budget) -> None:
    location = path.checked()
    if not path.workspace.policy.allows_path(location, write=True):
        raise EditRejected("write_denied", "Direct writing is denied by the configured or protected path policy.")
    parent = path.workspace.path(str(Path(path.relative).parent))
    rules = initial_rules(parent, budget)
    if rules is None or rules.excluded(path, False):
        raise EditRejected("ignored_path", "Gitignore rules make this path read-only for direct edits.")


def _publish(path: ScopedPath, before: FileSnapshot, updated: bytes, budget: Budget, result: FileEdit) -> None:
    target = path.checked()
    descriptor, temporary = tempfile.mkstemp(prefix=".local-dev-mcp-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            for offset in range(0, len(updated), 65536):
                budget.check()
                stream.write(updated[offset : offset + 65536])
            stream.flush()
            os.fchmod(stream.fileno(), stat.S_IMODE(before.info.st_mode))
            os.fsync(stream.fileno())
        require_writable(path, budget)
        latest = snapshot_file(path, budget)
        if latest.version != before.version or identity(latest.info) != identity(before.info):
            raise EditRejected("stale_version", "The file changed before replacement. Read and reconstruct the edit.")
        budget.check()
        # The comparison and replacement are separate OS operations; external editors do not share our lock.
        os.replace(temporary, path.checked())
        result.changed = True
        result.outcome = "applied"
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            result.cleanup_warning = "A temporary file could not be removed. Ask the workspace owner to inspect it."


def _read_next(result: FileEdit) -> None:
    result.next_call = NextCall(
        tool="read_workspace_file", arguments={"path": result.path, "start_line": result.start_line, "max_lines": 200}
    )
    result.next_action = (
        "Read the current file, locate the intended text again and reconstruct the edit using the new read_ref. "
        "Old line numbers may have shifted. Do not replay the old replacement blindly."
    )


def _observe(result: FileEdit, path: ScopedPath, references: ReadStore, record: StoredRead, budget: Budget) -> bool:
    content = result.content or ""
    lines = content.count("\n") + int(bool(content) and not content.endswith("\n"))
    observed = read_file(path, record.start_line, min(500, max(1, lines)), budget)
    if observed.version != result.version_after:
        raise EditRejected("changed_after_replace", "The file changed after this request's replacement.")
    if not observed.content and observed.total_lines and observed.start_line > observed.total_lines:
        observed = read_file(path, min(record.start_line, observed.total_lines), 1, budget)
        if observed.version != result.version_after:
            raise EditRejected("changed_after_replace", "The file changed after this request's replacement.")
    result.content = observed.content
    result.start_line, result.end_line = observed.start_line, observed.end_line
    returned_lines = (observed.end_line - observed.start_line + 1) if observed.end_line is not None else 0
    complete = observed.complete or (observed.reason == "line_limit" and returned_lines >= max(1, lines))
    if not complete:
        result.next_call = NextCall(
            tool="read_workspace_file",
            arguments={
                "path": result.path,
                "start_line": observed.next_start_line or result.start_line,
                "max_lines": 200,
            },
        )
        result.next_action = "The returned read_ref covers only the returned complete lines. Read more if needed."
    else:
        result.next_action = "Continue using the returned observed content and its new read_ref."
        result.next_call = None
    if len(result.model_dump_json().encode("utf-8")) > RESPONSE_BYTES - 128:
        raise WorkspaceError.response()
    result.read_ref = references.put(path, observed)
    return complete


def require_readable_result(updated: bytes, path: ScopedPath, start_line: int, replacement: bytes) -> None:
    # A successful edit must not itself create a line that no subsequent read can observe.
    for number, line in following_lines(updated, start_line, replacement.count(b"\n") + 1):
        if len(line) > BODY_BYTES:
            raise EditRejected("invalid_replacement", "An edited line would exceed the 16 KiB read limit.")
        candidate = FileRead(project=str(path.workspace.policy.root), path=path.relative, start_line=number)
        candidate.content = line.decode("utf-8")
        if not candidate.fits():
            raise EditRejected("invalid_replacement", "An edited line would exceed the structured read response limit.")


def replace_file(
    workspace: Workspace,
    references: ReadStore,
    locks: WriteLocks,
    value: str,
    token: str,
    replacement: str,
    budget: Budget,
) -> FileEdit:
    # Invalid path syntax and protected names use the same safe MCP error as reads.
    path = workspace.path(value)
    result = FileEdit(project=str(workspace.policy.root), path=path.relative)
    _read_next(result)
    if len(result.model_dump_json().encode("utf-8")) > RESPONSE_BYTES - 1024:
        raise WorkspaceError.response()
    try:
        if not workspace.policy.allows_path(workspace.policy.root / path.relative, write=True):
            raise EditRejected("write_denied", "Direct writing is denied by the configured or protected path policy.")
        data = replacement_bytes(replacement)
        with locks.hold(path.relative.casefold(), budget):
            require_writable(path, budget)
            try:
                record = references.get(token, path)
            except WorkspaceError:
                raise EditRejected(
                    "read_ref_unavailable", "The read_ref is expired, evicted or belongs to another file/server."
                ) from None
            result.start_line = record.start_line
            result.version_before = record.version
            _read_next(result)
            current = snapshot_file(path, budget)
            if current.version != record.version:
                raise EditRejected("stale_version", "The current file differs from the observed version.")
            updated = replace_span(current.data, record.content, record.start_line, record.end_line, data)
            require_readable_result(updated, path, record.start_line, data)
            result.version_after = hashlib.sha256(updated).hexdigest()
            result.content = replacement
            if updated != current.data:
                _publish(path, current, updated, budget, result)
            else:
                result.outcome = "unchanged"
            try:
                complete = _observe(result, path, references, record, budget)
            except WorkspaceError, EditRejected, OSError, ScanStopped:
                if result.changed:
                    result.outcome = "applied_unverified"
                result.code = "observation_failed"
                result.reason = "Replacement state is known, but the subsequent observation could not be completed."
                result.read_ref = None
                result.content = None
                _read_next(result)
                return result
            if complete:
                result.next_action = "Continue using the returned observed content and its new read_ref."
                result.next_call = None
            return result
    except EditRejected as error:
        result.code, result.reason = error.code, error.reason
    except WorkspaceError:
        result.code = "path_unavailable"
        result.reason = "The path cannot be read safely. Check path, type and permissions."
    except OSError:
        result.code, result.reason = "io_before_replace", "Local I/O failed before this request replaced the file."
    except ScanStopped:
        result.code, result.reason = "execution_budget", "The edit exceeded its execution budget before replacement."
    result.content = None
    result.version_after = None
    result.read_ref = None
    _read_next(result)
    if result.code in ("write_denied", "ignored_path"):
        result.next_call = None
        result.next_action = (
            "Review filesystem path permissions or gitignore rules with the workspace owner; do not retry unchanged."
        )
    elif result.code == "invalid_replacement":
        result.next_call = None
        result.next_action = (
            "Correct replacement using the reported UTF-8, NUL, size or line constraint. "
            "If the file has changed, read again and reconstruct the edit first."
        )
    elif result.code == "file_too_large":
        result.next_call = None
        result.next_action = "Choose a file within the supported size limit; do not repeat this edit unchanged."
    elif result.code == "path_unavailable":
        result.next_call = NextCall(tool="find_workspace_paths", arguments={"pattern": Path(path.relative).name})
        result.next_action = (
            "Check whether the file was removed, renamed or became inaccessible. "
            "Locate an existing permitted file, read it and reconstruct the edit; do not recreate it automatically."
        )
    return result
