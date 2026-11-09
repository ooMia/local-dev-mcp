"""Resolve only owned linked worktrees under the selected workspace."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from fastmcp.exceptions import ToolError

from ...settings import protected_name
from ._execution import GitExecution
from ._remote import feature_name, valid_sha
from .commands import status, worktree
from .facades import _repository
from .facades._refs import resolve_local_ref

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


class WorktreeState(TypedDict):
    project: str
    branch: str
    path: str
    state: Literal["absent", "partial", "ready"]
    head: str | None
    branch_head: str | None
    clean: bool | None
    registered: bool
    next_action: str
    observed_at: str


def require_permission(settings: ServerSettings, *, write: bool = False) -> None:
    try:
        settings.policy.require_project_access()
        settings.policy.require_command("git")
        if write:
            settings.policy.require_command("git.worktree.write")
    except PermissionError as error:
        raise ToolError(str(error)) from None


def locations(settings: ServerSettings, branch: str, *, write: bool = False) -> tuple[Path, Path]:
    feature_name(branch)
    root = settings.policy.root
    key = hashlib.sha256(branch.encode("ascii")).hexdigest()[:24]
    path = root / ".lab" / "worktrees" / key
    receipt = path.with_suffix(".json")
    for candidate in (root / ".lab", path.parent, path, receipt):
        if candidate.is_symlink():
            raise ToolError("Managed worktree paths must not traverse symlinks.")
        try:
            settings.policy.require_path_access(candidate, write=write)
        except PermissionError as error:
            raise ToolError(str(error)) from None
        if candidate not in (path, receipt) and candidate.exists() and not candidate.is_dir():
            raise ToolError("Managed worktree parent is not a directory.")
    return path, receipt


def scoped_settings(settings: ServerSettings, path: Path) -> ServerSettings:
    """Keep startup policy/environment; do not load historical config or .env."""
    original = settings.policy

    def mapped(scopes: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(
            path if original.root.is_relative_to(scope) else path / scope.relative_to(original.root)
            for scope in scopes
            if original.root.is_relative_to(scope) or scope.is_relative_to(original.root)
        )

    policy = replace(
        original,
        root=path,
        allowed_paths=mapped(original.allowed_paths),
        read_only_paths=mapped(original.read_only_paths),
        denied_paths=mapped(original.denied_paths),
    )
    return replace(settings, policy=policy)


def read_receipt(receipt: Path, branch: str) -> bool:
    if not receipt.is_file() or receipt.stat().st_size > 2048:
        return False
    try:
        record: object = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return False
        fields = cast(dict[str, object], record)
        return fields.get("schema") == 1 and fields.get("branch") == branch and fields.get("prepared") is True
    except OSError, UnicodeError, ValueError:
        return False


def write_receipt(receipt: Path, branch: str, head: str, *, prepared: bool) -> None:
    record = json.dumps({"schema": 1, "branch": branch, "source_head": head, "prepared": prepared}) + "\n"
    if not prepared:
        with receipt.open("x", encoding="utf-8") as stream:
            stream.write(record)
        return
    temporary = receipt.with_suffix(".json.prepared")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(record)
    temporary.replace(receipt)


def _dirty_contents_digest(settings: ServerSettings, entries: list[status.StatusEntry]) -> str:
    """Fingerprint visible dirty files without following links or returning file contents."""
    if len(entries) > 256:
        raise ToolError("Too many dirty paths to verify safely.")
    digest = hashlib.sha256()
    digest.update(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    total_bytes = 0
    for entry in entries:
        relative = Path(entry["path"])
        candidate = settings.policy.root / relative
        if any(protected_name(part) for part in relative.parts):
            raise ToolError("A protected dirty path prevents original-state verification.")
        try:
            settings.policy.require_path_access(candidate)
            metadata = candidate.lstat()
        except FileNotFoundError, NotADirectoryError:
            digest.update(b"missing")
            continue
        except (PermissionError, OSError) as error:
            raise ToolError("Cannot safely inspect a dirty path under the workspace policy.") from error
        digest.update(entry["path"].encode("utf-8", errors="surrogateescape"))
        if stat.S_ISLNK(metadata.st_mode):
            try:
                digest.update(b"link")
                digest.update(os.readlink(candidate).encode("utf-8", errors="surrogateescape"))
            except OSError as error:
                raise ToolError("Cannot verify a changed symlink.") from error
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ToolError("A non-regular dirty path prevents original-state verification.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            with os.fdopen(os.open(candidate, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                total_bytes += opened.st_size
                if not stat.S_ISREG(opened.st_mode) or total_bytes > 64 * 1024 * 1024:
                    raise ToolError("Dirty snapshot exceeds the safe regular-file limit.")
                digest.update(b"file")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                finished = os.fstat(stream.fileno())
                if (opened.st_size, opened.st_mtime_ns) != (finished.st_size, finished.st_mtime_ns):
                    raise ToolError("A dirty file changed during snapshot inspection.")
        except OSError as error:
            raise ToolError("Cannot safely read a dirty file for state verification.") from error
    return digest.hexdigest()


async def original_snapshot(settings: ServerSettings, branch: str, head: str, *, allow_dirty: bool = False) -> str:
    repo = GitExecution(settings)
    await _repository.verify_root(repo)
    await _repository.require_writable_state(repo)
    actual_branch, actual_head, entries = await status.read_porcelain_v2(repo)
    if actual_branch != branch or actual_head != valid_sha(head):
        raise ToolError("Original checkout branch or HEAD changed. Re-observe status and HEAD.")
    if entries and not allow_dirty:
        raise ToolError("Original checkout changed or is not clean. Re-observe status and HEAD.")
    if any(entry["kind"] == "unmerged" for entry in entries):
        raise ToolError("Unmerged original index prevents worktree operations.")
    index = await repo.run("ls-files", "--stage", "-z", operation="Observe original index")
    if not entries:
        return hashlib.sha256(index.output).hexdigest()
    digest = hashlib.sha256(index.output)
    digest.update(b"dirty-original-v1")
    digest.update(_dirty_contents_digest(settings, entries).encode("ascii"))
    return digest.hexdigest()


async def confirm_original(
    settings: ServerSettings, branch: str, head: str, fingerprint: str, *, allow_dirty: bool = False
) -> None:
    if await original_snapshot(settings, branch, head, allow_dirty=allow_dirty) != fingerprint:
        raise ToolError("Original checkout state changed; inspect both worktrees before continuing.")


def bound_git_pointer(path: Path, common: Path) -> bool:
    """Check the admin directory and its backlink before executing Git in a target."""
    pointer = path / ".git"
    try:
        if pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 4096:
            return False
        value = pointer.read_text(encoding="utf-8").removesuffix("\n")
        if not value.startswith("gitdir: ") or "\n" in value or "\r" in value:
            return False
        directory = Path(value[8:])
        directory = directory if directory.is_absolute() else path / directory
        if directory.is_symlink():
            return False
        directory = directory.resolve(strict=True)
        if directory.parent != common / "worktrees":
            return False
        backlink = directory / "gitdir"
        if backlink.is_symlink() or not backlink.is_file() or backlink.stat().st_size > 4096:
            return False
        linked = Path(backlink.read_text(encoding="utf-8").removesuffix("\n"))
        linked = linked if linked.is_absolute() else directory / linked
        return linked.resolve() == pointer
    except OSError, UnicodeError, ValueError:
        return False


async def inspect(settings: ServerSettings, branch: str) -> WorktreeState:
    require_permission(settings)
    path, receipt = locations(settings, branch)
    repo = GitExecution(settings)
    await _repository.verify_root(repo)
    records = await worktree.list_registered(repo)
    selected = [row for row in records if row.path == path]
    branch_head: str | None = None
    try:
        _, branch_head = await resolve_local_ref(repo, branch)
    except ToolError:
        pass
    result: WorktreeState = {
        "project": str(repo.root),
        "branch": branch,
        "path": path.relative_to(repo.root).as_posix(),
        "state": "absent",
        "head": None,
        "branch_head": branch_head,
        "clean": None,
        "registered": bool(selected),
        "next_action": "Prepare the observed local topic branch.",
        "observed_at": _repository.observed_at(),
    }
    if not path.exists() and not receipt.exists() and not selected:
        return result
    result["state"] = "partial"
    result["next_action"] = (
        "Inspect retained files and Git registration; do not retry preparation or delete automatically."
    )
    if len(selected) != 1 or selected[0].unavailable or not path.is_dir() or not read_receipt(receipt, branch):
        return result
    if not bound_git_pointer(path, await worktree.common_directory(repo)):
        return result
    target = GitExecution(scoped_settings(settings, path))
    try:
        await _repository.verify_root(target)
        if await worktree.common_directory(target) != await worktree.common_directory(repo):
            return result
        actual_branch, head, entries = await status.read_porcelain_v2(target)
        result["head"] = head
        if actual_branch != branch or selected[0].branch != "refs/heads/" + branch or head != branch_head:
            return result
        await _repository.require_writable_state(target)
    except ToolError:
        return result
    result["state"] = "ready"
    result["clean"] = not entries
    result["next_action"] = (
        "Observe remote refs before publication."
        if not entries
        else "Review target worktree changes before publication."
    )
    return result


async def require_ready(settings: ServerSettings, branch: str, head: str) -> ServerSettings:
    state = await inspect(settings, branch)
    if state["state"] != "ready" or state["clean"] is not True or state["head"] != valid_sha(head):
        raise ToolError("Target worktree is unprepared, dirty, partial or its branch HEAD changed. Inspect it first.")
    path, _ = locations(settings, branch)
    return scoped_settings(settings, path)
