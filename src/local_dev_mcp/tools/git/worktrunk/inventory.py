"""Policy-filtered Git inventory with optional, independently verified Worktrunk facts."""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastmcp.exceptions import ToolError

from .. import _worktrees
from .._execution import GitExecution, text_output
from ..commands import status, worktree
from ..facades import _repository
from .adapter import WorktrunkAdapter

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_LIMIT = 128


def _safe_path(settings: ServerSettings, path: Path) -> bool:
    root = settings.policy.root
    if not path.is_absolute() or not path.is_relative_to(root):
        return False
    try:
        if not path.is_dir() or path.is_symlink() or path.resolve() != path:
            return False
        settings.policy.require_path_access(path)
    except OSError, PermissionError, ValueError:
        return False
    return True


def _wt_facts(data: dict[str, object], rows: list[dict[str, object]]) -> bool:
    """Use only local, checked-out rows with exact Git-matching path and SHA."""
    raw: object = data.get("items")
    if data.get("schema") != 2 or not isinstance(raw, list):
        return False
    index: dict[tuple[str, str], dict[str, object]] = {}
    for item_object in cast(list[object], raw):
        if not isinstance(item_object, dict):
            return False
        item = cast(dict[str, object], item_object)
        branch = item.get("branch")
        location = item.get("worktree")
        if not isinstance(branch, str) or not isinstance(location, dict):
            continue  # Branch-only rows have no checkout metadata.
        path = cast(dict[str, object], location).get("path")
        if not isinstance(path, str):
            return False
        if (branch, path) in index:
            return False
        index[(branch, path)] = item
    pending: list[tuple[dict[str, object], dict[str, object]]] = []
    for row in rows:
        if not row["registered"] or row["state"] == "blocked":
            continue
        branch = row["branch"]
        if not isinstance(branch, str):
            continue
        absolute = str(row["absolute_path"])
        item = index.get((branch, absolute))
        if item is None:
            return False
        head = item.get("head")
        if not isinstance(head, dict) or cast(dict[str, object], head).get("sha") != row["head"]:
            return False
        values: dict[str, object] = {}
        default = item.get("default_branch")
        if isinstance(default, dict):
            default = cast(dict[str, object], default)
            for key in ("ahead", "behind"):
                value = default.get(key)
                if type(value) is int and value >= 0:
                    values[key] = value
            if isinstance(default.get("merge_conflicts"), bool):
                values["merge_conflicts"] = default["merge_conflicts"]
        location = item.get("worktree")
        changes = cast(dict[str, object], location).get("changes") if isinstance(location, dict) else None
        if isinstance(changes, dict):
            changes = cast(dict[str, object], changes)
            if isinstance(changes.get("conflicted"), bool):
                values["conflicted"] = changes["conflicted"]
        pending.append((row, values))
    for row, values in pending:
        row.update(values)
    return True


async def inventory(
    settings: ServerSettings, *, include_branches: bool = True, include_remote: bool = False
) -> dict[str, object]:
    """Only expose validated paths; an optional CLI cannot authorize any Git row."""
    if include_remote:
        raise ToolError("Remote-only branches require a separate remote authorization and are not supported.")
    _worktrees.require_permission(settings)
    repo = GitExecution(settings)
    await _repository.verify_root(repo)
    registered = await worktree.list_registered(repo)
    common = await worktree.common_directory(repo)
    rows: list[dict[str, object]] = []
    blocked = 0
    known: set[str] = set()
    blocked_branches: set[str] = set()
    complete = len(registered) <= _LIMIT
    for record in registered[:_LIMIT]:
        path = record.path
        if record.branch is not None and record.branch.startswith("refs/heads/"):
            blocked_branches.add(record.branch.removeprefix("refs/heads/"))
        if record.unavailable or not _safe_path(settings, path):
            blocked += 1
            continue
        target = GitExecution(_worktrees.scoped_settings(settings, path))
        try:
            await _repository.verify_root(target)
            if await worktree.common_directory(target) != common:
                blocked += 1
                continue
            branch, head, entries = await status.read_porcelain_v2(target)
        except ToolError:
            blocked += 1
            continue
        if head is None or _SHA.fullmatch(head) is None or head != record.head:
            blocked += 1
            continue
        if record.branch != (f"refs/heads/{branch}" if branch else None):
            blocked += 1
            continue
        if branch is not None:
            known.add(branch)
            blocked_branches.discard(branch)
        rows.append(
            {
                "branch": branch,
                "head": head,
                "path": path.relative_to(repo.root).as_posix() if path != repo.root else ".",
                "absolute_path": path,  # Internal: removed before returning.
                "state": "dirty" if entries else "ready",
                "registered": True,
                "clean": not entries,
                "ahead": None,
                "behind": None,
                "merge_conflicts": None,
                "conflicted": any(entry["kind"] == "unmerged" for entry in entries),
            }
        )
    if include_branches:
        result = await repo.run(
            "for-each-ref",
            "--format=%(refname)%00%(objectname)",
            "refs/heads/",
            operation="List local Git branches",
        )
        for line in text_output(result.output).splitlines():
            parts = line.split("\0")
            if len(parts) != 2 or not parts[0].startswith("refs/heads/") or _SHA.fullmatch(parts[1]) is None:
                raise ToolError("Git returned an invalid local branch record.")
            name = parts[0][len("refs/heads/") :]
            if name in known or name in blocked_branches:
                continue
            if len(rows) >= _LIMIT:
                complete = False
                break
            rows.append(
                {
                    "branch": name,
                    "head": parts[1],
                    "path": None,
                    "state": "not_checked_out",
                    "registered": False,
                    "clean": None,
                    "ahead": None,
                    "behind": None,
                    "merge_conflicts": None,
                    "conflicted": None,
                }
            )
    enrichment = "not_available"
    if (
        "wt" in settings.policy.allowed_commands
        and shutil.which("wt", path=dict(settings.environment).get("PATH")) is not None
    ):
        try:
            data = await WorktrunkAdapter(settings).list_worktrees(include_branches=include_branches)
            if _wt_facts(data, rows):
                enrichment = "verified"
            else:
                enrichment = "schema_or_identity_mismatch"
                complete = False
        except ToolError:
            enrichment = "unavailable"
            complete = False
    for row in rows:
        row.pop("absolute_path", None)
    return {
        "project": str(repo.root),
        "items": rows,
        "blocked_count": blocked,
        "complete": complete,
        "reason": None
        if complete
        else ("result_limit" if len(registered) > _LIMIT or len(rows) >= _LIMIT else enrichment),
        "enrichment": enrichment,
        "observed_at": datetime.now(UTC).isoformat(),
    }
