"""Create and delete one regular text file with explicit, server-local submission identity."""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal

from ._access import ScopedPath, Workspace, protected
from ._discovery import IgnoreRules
from ._edits import EditRejected, replacement_bytes
from ._mutation_response import FileMutation
from ._read_refs import ReadStore
from ._reads import Budget, ScanStopped, identity, read_file
from ._receipts import MutationReceipts, Receipt
from ._response import RESPONSE_BYTES, NextCall
from ._writes import WriteLocks, require_readable_result, require_writable, snapshot_file
from .errors import WorkspaceError


def _relative(value: str) -> str:
    # Pure validation must precede receipt lookup, without touching a path that
    # may have disappeared or become a symlink after the original submission.
    if not value or len(value) > 1024 or "\0" in value or Path(value).is_absolute():
        raise WorkspaceError.path()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise WorkspaceError.path() from None
    parts = Path(value).parts
    if not parts or any(part == ".." for part in parts):
        raise WorkspaceError.path()
    if any(protected(part) for part in parts):
        raise WorkspaceError.protected()
    relative = Path(value).as_posix()
    if relative == ".":
        raise WorkspaceError("File creation and deletion require a file path, not the workspace root.")
    return relative


def _fingerprint(operation: str, path: str, value: str, create_parents: bool = False) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return hashlib.sha256(json.dumps([operation, path, digest, create_parents]).encode()).hexdigest()


def _bounded(result: FileMutation) -> None:
    if len(result.model_dump_json().encode("utf-8")) > RESPONSE_BYTES - 1024:
        raise WorkspaceError.response()


class FileMutations:
    def __init__(self, workspace: Workspace, references: ReadStore, locks: WriteLocks) -> None:
        self.workspace = workspace
        self.references = references
        self.locks = locks
        self.receipts = MutationReceipts()

    def _begin(
        self,
        operation: Literal["create", "delete"],
        path: str,
        value: str,
        request_id: str,
        create_parents: bool,
        budget: Budget,
    ) -> tuple[FileMutation, Receipt | FileMutation]:
        # The concrete receipt is returned separately so only its owner finalizes it.
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) is None:
            raise WorkspaceError.arguments(
                "request_id", "Use 1..128 ASCII letters, digits, dots, underscores, colons or hyphens."
            )
        relative = _relative(path)
        result = FileMutation(
            project=str(self.workspace.policy.root), path=relative, operation=operation, request_id=request_id
        )
        _bounded(result)
        claim = self.receipts.claim(request_id, _fingerprint(operation, relative, value, create_parents), budget)
        return result, claim

    def create(
        self,
        path: str,
        content: str,
        request_id: str,
        create_parents: bool,
        budget: Budget,
    ) -> FileMutation:
        result, claim = self._begin("create", path, content, request_id, create_parents, budget)
        if isinstance(claim, FileMutation):
            return claim
        # The replay case returned above; the remaining claim is the owning receipt.
        # Handle any additional _begin claim variants explicitly before finalizing them.
        try:
            try:
                data = replacement_bytes(content)
            except EditRejected:
                raise EditRejected(
                    "invalid_content", "content must be UTF-8 without NUL and fit within 16 KiB."
                ) from None
            scoped = self.workspace.path(path)
            try:
                require_readable_result(data, scoped, 1, data)
            except EditRejected:
                raise EditRejected(
                    "invalid_content", "A content line would exceed the structured read response limit."
                ) from None
            with self.locks.hold(scoped.relative.casefold(), budget):
                target = self._creation_target(scoped, create_parents, result, budget)
                self._absent(target)
                self._publish(scoped, data, result, budget)
                self._observe_created(scoped, result, budget)
        except EditRejected as error:
            self._rejected(result, error.code, error.reason, content=content)
        except WorkspaceError, OSError:
            self._rejected(result, "path_unavailable", "File creation could not access a permitted regular path.")
        except ScanStopped:
            self._rejected(result, "execution_budget", "The creation exceeded its execution budget.")
        except BaseException:
            self._interrupted(result)
            raise
        finally:
            self._finalize_result(result)
            self.receipts.finish(claim, result)
        return result

    def delete(self, path: str, read_ref: str, request_id: str, budget: Budget) -> FileMutation:
        if not read_ref or len(read_ref) > 256 or "\0" in read_ref:
            raise WorkspaceError.arguments("read_ref", "Use a valid observation for this file and server.")
        result, claim = self._begin("delete", path, read_ref, request_id, False, budget)
        if isinstance(claim, FileMutation):
            return claim
        # The replay case returned above; the remaining claim is the owning receipt.
        # Handle any additional _begin claim variants explicitly before finalizing them.
        try:
            scoped = self.workspace.path(path)
            with self.locks.hold(scoped.relative.casefold(), budget):
                require_writable(scoped, budget)
                try:
                    observed = self.references.get(read_ref, scoped)
                except WorkspaceError:
                    raise EditRejected(
                        "read_ref_unavailable", "The read_ref is expired, evicted or unavailable."
                    ) from None
                current = snapshot_file(scoped, budget)
                result.version_before = observed.version
                if current.version != observed.version:
                    raise EditRejected(
                        "stale_version", "The current file differs from the observed whole-file version."
                    )
                require_writable(scoped, budget)
                latest = snapshot_file(scoped, budget)
                if latest.version != current.version or identity(latest.info) != identity(current.info):
                    raise EditRejected("stale_version", "The file changed before deletion; read its current state.")
                budget.check()
                # External editors do not share this lock; the comparison and
                # unlink are separate operations, not an OS compare-and-swap.
                os.unlink(scoped.checked())
                result.changed = True
                result.outcome = "applied"
                result.next_action = "Deletion completed without backup. Inspect the working tree before continuing."
                budget.check()
                try:
                    (self.workspace.policy.root / scoped.relative).lstat()
                except FileNotFoundError:
                    pass
                else:
                    self._rejected(result, "recreated_after_delete", "The path exists again after deletion.")
        except EditRejected as error:
            self._rejected(result, error.code, error.reason)
        except WorkspaceError, OSError:
            self._rejected(result, "path_unavailable", "Deletion could not access a permitted regular text file.")
        except ScanStopped:
            self._rejected(result, "execution_budget", "The deletion exceeded its execution budget.")
        except BaseException:
            self._interrupted(result)
            raise
        finally:
            self._finalize_result(result)
            self.receipts.finish(claim, result)
        return result

    def _creation_target(
        self,
        path: ScopedPath,
        parents: bool,
        result: FileMutation,
        budget: Budget,
    ) -> Path:
        self.workspace.require_access()
        self._write_policy(path)
        current = self.workspace.path(".")
        rules = IgnoreRules().enter(current, budget)
        for part in Path(path.relative).parts[:-1]:
            budget.check()
            directory = current.join(part)
            if rules.excluded(directory, True):
                raise EditRejected("ignored_path", "Gitignore rules make the destination directory read-only.")
            location = self.workspace.policy.root / directory.relative
            try:
                info = location.lstat()
            except FileNotFoundError as err:
                if not parents:
                    raise EditRejected(
                        "missing_parent", "A parent directory is missing; creation was not requested."
                    ) from err
                self._write_policy(directory)
                candidate = result.model_copy(deep=True)
                candidate.created_directories.append(directory.relative)
                if len(candidate.model_dump_json().encode("utf-8")) > RESPONSE_BYTES // 2:
                    raise EditRejected(
                        "directory_metadata_limit", "The created-directory receipt would exceed its limit."
                    ) from err
                budget.check()
                try:
                    os.mkdir(location)
                except FileExistsError:
                    pass
                else:
                    result.created_directories.append(directory.relative)
                info = location.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise EditRejected("path_unavailable", "The destination parent is not a regular directory.")
            directory.checked()
            rules = rules.enter(directory, budget)
            current = directory
        if rules.excluded(path, False):
            raise EditRejected("ignored_path", "Gitignore rules make the destination read-only.")
        self._write_policy(path)
        return current.checked() / Path(path.relative).name

    def _write_policy(self, path: ScopedPath) -> None:
        if not self.workspace.policy.allows_path(self.workspace.policy.root / path.relative, write=True):
            raise EditRejected("write_denied", "Writing this path is denied by the configured or protected policy.")

    @staticmethod
    def _absent(target: Path) -> None:
        try:
            target.lstat()
        except FileNotFoundError:
            return
        raise EditRejected("path_exists", "Creation requires an absent path; existing entries are never replaced.")

    def _publish(self, path: ScopedPath, data: bytes, result: FileMutation, budget: Budget) -> None:
        target = self._creation_target(path, False, result, budget)
        descriptor, temporary = tempfile.mkstemp(prefix=".local-dev-mcp-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                budget.check()
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            target = self._creation_target(path, False, result, budget)
            budget.check()
            # Link a complete same-directory file without replacing any entry.
            # Unsupported hard links fail closed instead of falling back to replace.
            os.link(temporary, target, follow_symlinks=False)
            result.changed = True
            result.outcome = "applied"
            result.version_after = hashlib.sha256(data).hexdigest()
        except FileExistsError:
            raise EditRejected(
                "path_exists", "The destination appeared before publication; nothing was overwritten."
            ) from None
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                result.cleanup_warning = "A temporary file could not be removed. Ask the workspace owner to inspect it."

    def _observe_created(self, path: ScopedPath, result: FileMutation, budget: Budget) -> None:
        observed = read_file(path, 1, 500, budget)
        if observed.version != result.version_after:
            raise EditRejected("changed_after_create", "The created file changed before read-back completed.")
        result.content = observed.content
        result.start_line = observed.start_line
        result.end_line = observed.end_line
        if observed.complete:
            result.next_action = "Continue from the returned observed content and its read_ref."
        else:
            result.next_call = NextCall(
                tool="read_workspace_file",
                arguments={"path": result.path, "start_line": observed.next_start_line or 1, "max_lines": 200},
            )
            result.next_action = "The read_ref covers only the returned range. Read the next range before editing it."
        _bounded(result)
        result.read_ref = self.references.put(path, observed)

    @staticmethod
    def _rejected(result: FileMutation, code: str, reason: str, *, content: str | None = None) -> None:
        result.code, result.reason = code, reason
        result.content = None
        result.read_ref = None
        if result.changed:
            result.outcome = "applied_unverified"
            result.next_action = (
                "The mutation took effect. Inspect the current path before a NEW request; do not replay it."
            )
        else:
            result.outcome = "rejected"
            result.next_action = "Inspect the cause and current files. Use a NEW request_id for corrected arguments."
        if code == "missing_parent" and content is not None:
            result.next_action = "Use the original content with create_parents=true and a NEW request_id."
            result.next_call = NextCall(
                tool="create_workspace_file",
                arguments={
                    "path": result.path,
                    "content": content,
                    "create_parents": True,
                    "request_id": token_urlsafe(24),
                },
            )
            if len(result.model_dump_json().encode("utf-8")) > RESPONSE_BYTES - 1024:
                result.next_call = None
        elif code not in ("write_denied", "ignored_path", "invalid_content", "path_exists"):
            result.next_call = NextCall(tool="read_workspace_file", arguments={"path": result.path})

    @staticmethod
    def _interrupted(result: FileMutation) -> None:
        FileMutations._rejected(
            result, "operation_interrupted", "The request was interrupted; its receipt preserves known state."
        )

    @staticmethod
    def _finalize_result(result: FileMutation) -> None:
        if len(result.model_dump_json().encode("utf-8")) <= RESPONSE_BYTES - 128:
            return
        # Preserve the mutation state and directory effects even when optional
        # observation content or a corrective call cannot fit in the receipt.
        if result.content is not None:
            result.content = None
            result.read_ref = None
            if result.changed:
                result.outcome = "applied_unverified"
        result.next_call = None
        result.next_action = (
            "Inspect the current path. Use a NEW request_id for a corrected or intentional new mutation."
        )

    def clear(self) -> None:
        self.receipts.clear()
