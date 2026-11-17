"""Explicit developer operation: prepare files, never commit, tag or publish."""

import asyncio
import stat
from pathlib import Path
from typing import TypedDict

from ..uv import update_version
from ._git import LocalGit
from ._prepared import changelog_bytes, expected_receipt, receipt_bytes, verify_release
from ._snapshots import Snapshot, parse_toml, read_snapshot


class PreparedRelease(TypedDict):
    prepared: bool
    source_head: str
    plan_digest: str
    version: str
    consumed: list[str]
    commit_created: bool


def _regular(root: Path, relative: str, *, optional: bool = False) -> Path:
    path = root / relative
    parent = path.parent
    if parent.is_symlink() or parent.resolve() != parent or not parent.is_dir():
        raise ValueError("Release paths must remain inside ordinary workspace directories.")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if optional:
            return path
        raise ValueError("A required release input is missing.") from None
    if not stat.S_ISREG(mode):
        raise ValueError("Release inputs and outputs must be ordinary files, never symlinks.")
    return path


async def _baseline(repository: LocalGit, head: str, branch: str) -> None:
    if await repository.head() != head:
        raise ValueError("Release HEAD changed; observe and plan again.")
    if (await repository.read("branch", "--show-current")).decode().strip() != branch:
        raise ValueError("Prepare on release/<next-version> from the observed integration head.")
    if await repository.read("status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ValueError("Release preparation requires a clean index and working tree including untracked files.")


async def _changed_paths(repository: LocalGit, allowed: set[str]) -> list[str]:
    raw = (await repository.read("status", "--porcelain=v1", "-z", "--untracked-files=all")).decode()
    paths: list[str] = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        if len(entry) < 4 or (entry[:2] != "??" and entry[0] != " "):
            raise ValueError("The index or integration state changed during release preparation.")
        path = entry[3:]
        if path not in allowed:
            raise ValueError("Release preparation observed unrelated or unexpected working state.")
        paths.append(path)
    return paths


async def prepare_release(root: Path, expected_head: str, expected_plan: str) -> PreparedRelease:
    root = await asyncio.to_thread(root.resolve, strict=True)
    repository = LocalGit(root)
    if await repository.head() != expected_head:
        raise ValueError("Release HEAD differs from the observed source.")
    snapshot = await read_snapshot(repository.read, expected_head)
    plan = snapshot.plan()
    if plan["digest"] != expected_plan:
        raise ValueError("The cumulative release plan changed; inspect a fresh plan before preparing.")
    receipt = expected_receipt(snapshot)
    await _baseline(repository, expected_head, f"release/{receipt.new_version}")
    output_names = ("pyproject.toml", "uv.lock", "CHANGELOG.md", ".release.toml")
    for name in (*output_names, *(f".changes/{item}.toml" for item in snapshot.records)):
        path = _regular(root, name, optional=name in ("CHANGELOG.md", ".release.toml"))
        expected = snapshot.blobs.get(name)
        if path.exists() and (expected is None or path.read_bytes() != expected):
            raise ValueError("Release files differ from the observed snapshot; do not overwrite unreviewed content.")
    notes = changelog_bytes(snapshot.blobs.get("CHANGELOG.md"), plan)
    await _baseline(repository, expected_head, f"release/{receipt.new_version}")
    await update_version(root, receipt.new_version)
    # External tools may already have changed version/lock on failure; no reset
    # or replay follows. Re-check paths before touching other release outputs.
    for name in output_names:
        _regular(root, name, optional=name in ("CHANGELOG.md", ".release.toml"))
    if await repository.head() != expected_head:
        raise ValueError("HEAD changed after version preparation; inspect all partial state before retrying.")
    if (await repository.read("branch", "--show-current")).decode().strip() != f"release/{receipt.new_version}":
        raise ValueError("The branch changed during release preparation; inspect partial state.")
    await _changed_paths(repository, {"pyproject.toml", "uv.lock"})
    (root / "CHANGELOG.md").write_bytes(notes)
    (root / ".release.toml").write_bytes(receipt_bytes(receipt))
    for name in snapshot.records:
        relative = f".changes/{name}.toml"
        path = _regular(root, relative)
        if path.read_bytes() != snapshot.blobs[relative]:
            raise ValueError("An assessment changed during preparation; inspect partial state before retrying.")
        path.unlink()
    blobs = {name: (root / name).read_bytes() for name in output_names}
    project = parse_toml(blobs["pyproject.toml"])
    candidate = Snapshot(expected_head, snapshot.config, project, receipt.new_version, {}, blobs, snapshot.modes)
    allowed = set(output_names) | {f".changes/{name}.toml" for name in snapshot.records}
    changed = await _changed_paths(repository, allowed)
    verify_release(snapshot, candidate, changed)
    return {
        "prepared": True,
        "source_head": expected_head,
        "plan_digest": expected_plan,
        "version": receipt.new_version,
        "consumed": receipt.consumed,
        "commit_created": False,
    }
