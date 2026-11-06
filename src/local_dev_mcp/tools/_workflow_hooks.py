"""Repository-owned hook installation and read-only activation diagnostics."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, TypedDict

HOOK_NAMES = ("pre-commit", "commit-msg", "pre-push", "reference-transaction")
_MARKER = "# local-dev-mcp managed workflow hook"


class HooksBackup(TypedDict):
    local: list[str]
    worktrees: dict[str, list[str]]


class GitRunner(Protocol):
    def __call__(self, root: Path, *argv: str, capture: bool = False) -> str: ...


def _values(
    root: Path, environment: dict[str, str], key: str, *, scope: str | None = None, boolean: bool = False
) -> list[str]:
    options = ([] if scope is None else [f"--{scope}"]) + (["--type=bool"] if boolean else [])
    result = subprocess.run(
        ["git", "config", *options, "--null", "--get-all", key],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise ValueError(f"Cannot inspect Git configuration key {key}.")
    return result.stdout.removesuffix("\0").split("\0")


def _worktree_enabled(root: Path, environment: dict[str, str]) -> bool:
    values = _values(root, environment, "extensions.worktreeConfig", scope="local", boolean=True)
    return bool(values) and values[-1] == "true"


def _directory(root: Path, git: GitRunner) -> Path:
    common = Path(git(root, "git", "rev-parse", "--path-format=absolute", "--git-common-dir", capture=True).strip())
    return common / "local-dev-mcp" / "hooks"


def _wrapper(name: str) -> str:
    return (
        f'#!/bin/sh\n{_MARKER}\nexec {shlex.quote(sys.executable)} -m local_dev_mcp.tools.workflow hook {name} "$@"\n'
    )


def inspect_hooks(root: Path, git: GitRunner, environment: dict[str, str]) -> dict[str, object]:
    directory = _directory(root, git)
    configured = _values(root, environment, "core.hooksPath")
    raw = configured[-1] if configured else str(directory.parent.parent / "hooks")
    effective = Path(raw) if Path(raw).is_absolute() else root / raw
    valid: dict[str, bool] = {}
    for name in HOOK_NAMES:
        path = directory / name
        valid[name] = (
            path.is_file()
            and not path.is_symlink()
            and path.read_text() == _wrapper(name)
            and (os.name == "nt" or os.access(path, os.X_OK))
        )
    problems: list[str] = []
    if directory.parent.is_symlink() or directory.is_symlink():
        problems.append("The project-owned hook directory must not be a symlink.")
    if effective.resolve() != directory.resolve():
        problems.append("The effective core.hooksPath differs from the project-owned hook directory.")
    for name, okay in valid.items():
        if not okay:
            problems.append(f"{name} is missing, altered, nonexecutable or needs Python-path refresh.")
    return {
        "ready": not problems,
        "expected_hooks_path": str(directory),
        "effective_hooks_path": str(effective),
        "hooks": valid,
        "local_hooks_path": _values(root, environment, "core.hooksPath", scope="local"),
        "worktree_config_enabled": _worktree_enabled(root, environment),
        "backup_path": str(directory.parent / "hooks-config-backup.json"),
        "python": sys.executable,
        "problems": problems,
    }


def install_hooks(root: Path, git: GitRunner, environment: dict[str, str]) -> dict[str, object]:
    directory = _directory(root, git)
    if directory.parent.is_symlink() or directory.is_symlink():
        raise ValueError("The project-owned hook directory must not be a symlink.")
    for name in HOOK_NAMES:
        path = directory / name
        if path.is_symlink() or (
            path.exists() and (not path.is_file() or not path.read_text().startswith(f"#!/bin/sh\n{_MARKER}\n"))
        ):
            raise ValueError(f"Foreign file at project-owned {name}; it was not overwritten.")
    worktree = _worktree_enabled(root, environment)
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory.parent / "hooks-config-backup.json"
    if backup.is_symlink():
        raise ValueError("The hook configuration backup must not be a symlink.")
    state: HooksBackup
    if backup.exists():
        state = json.loads(backup.read_text())
    else:
        state = {"local": _values(root, environment, "core.hooksPath", scope="local"), "worktrees": {}}
    if worktree:
        git_dir = git(root, "git", "rev-parse", "--path-format=absolute", "--git-dir", capture=True).strip()
        if git_dir not in state["worktrees"]:
            state["worktrees"][git_dir] = _values(root, environment, "core.hooksPath", scope="worktree")
    with NamedTemporaryFile(mode="w", dir=backup.parent, delete=False) as stream:
        json.dump(state, stream, indent=2, sort_keys=True)
        temporary = Path(stream.name)
    temporary.replace(backup)
    for name in HOOK_NAMES:
        path = directory / name
        path.write_text(_wrapper(name))
        path.chmod(0o755)
    # Repository/worktree scopes override system/global settings without editing them.
    git(root, "git", "config", "--local", "--replace-all", "core.hooksPath", str(directory))
    if worktree:
        git(root, "git", "config", "--worktree", "--replace-all", "core.hooksPath", str(directory))
    result = inspect_hooks(root, git, environment)
    if result["ready"] is not True:
        raise ValueError("Hook files were prepared, but effective activation failed; run workflow doctor.")
    return result
