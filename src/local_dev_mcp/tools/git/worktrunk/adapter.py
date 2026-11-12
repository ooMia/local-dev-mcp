"""Opt-in Worktrunk boundary: fixed commands, bounded output and scoped worktrees.

This module does not replace the registered Git tools until isolated CLI and MCP
integration tests establish that Worktrunk can uphold their safety contracts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastmcp.exceptions import ToolError

from .._execution import GitExecution
from .._remote import feature_name, valid_sha
from .._worktrees import confirm_original, original_snapshot, require_permission
from ..commands import status, worktree
from ..facades._refs import resolve_local_ref

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

_OUTPUT_LIMIT = 1_048_576


def _managed_path(settings: ServerSettings, branch: str, *, write: bool) -> Path:
    feature_name(branch)
    root = settings.policy.root
    parent = root / ".lab" / "wt"
    path = parent / hashlib.sha256(branch.encode("ascii")).hexdigest()[:24]
    for candidate in (parent.parent, parent, path):
        if candidate.is_symlink():
            raise ToolError("Worktrunk managed paths cannot traverse symlinks.")
        if candidate != path and candidate.exists() and not candidate.is_dir():
            raise ToolError("Worktrunk managed parent is not a directory.")
        try:
            settings.policy.require_path_access(candidate, write=write)
        except PermissionError as error:
            raise ToolError(str(error)) from None
    return path


def _authorize(settings: ServerSettings, *, write: bool) -> None:
    require_permission(settings, write=write, backend_commands=("wt",))


def require_worktrunk_access(settings: ServerSettings, *, write: bool) -> None:
    """Expose the adapter's fixed logical access policy to submodules."""
    _authorize(settings, write=write)


@dataclass(frozen=True)
class WorktrunkAdapter:
    settings: ServerSettings
    binary: str | None = None

    async def _run(self, *argv: str, write: bool) -> dict[str, Any] | list[Any]:
        _authorize(self.settings, write=write)
        env = dict(self.settings.environment)
        executable = self.binary or shutil.which("wt", path=env.get("PATH"))
        if executable is None:
            raise ToolError("Worktrunk CLI (wt) is not installed or unavailable to the MCP process.")
        command = [executable, "-C", str(self.settings.policy.root), *argv, "--format", "json"]
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**env, "GIT_TERMINAL_PROMPT": "0"},
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60 if write else 15)
        except TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise ToolError("Worktrunk timed out; reobserve Git refs and worktrees before any retry.") from None
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except OSError:
            raise ToolError("Worktrunk process could not be started or observed.") from None
        if len(stdout) > _OUTPUT_LIMIT:
            raise ToolError("Worktrunk output exceeded the 1 MiB limit.")
        try:
            response = json.loads(stdout)
        except ValueError, UnicodeDecodeError:
            if process.returncode != 0:
                raise ToolError("Worktrunk failed; state may have changed. Reobserve before retrying.") from None
            raise ToolError("Worktrunk did not return valid JSON.") from None
        if not isinstance(response, (dict, list)):
            raise ToolError("Worktrunk returned an unsupported JSON result.")
        if process.returncode != 0:
            raise ToolError("Worktrunk did not complete successfully; inspect Git state before retrying.")
        return cast(dict[str, Any] | list[Any], response)

    async def list_worktrees(self) -> dict[str, Any]:
        data = await self._run("list", "--no-progressive", write=False)
        if not isinstance(data, dict) or data.get("schema") != 2 or not isinstance(data.get("items"), list):
            raise ToolError("Unsupported Worktrunk list schema; expected schema 2.")
        return data

    async def require_ready(self, branch: str, expected_head: str) -> ServerSettings:
        """Bind an exact clean Worktrunk checkout to the controller's startup policy."""
        _authorize(self.settings, write=True)
        from .. import _worktrees
        from ..facades import _repository

        repo = GitExecution(self.settings)
        path = _managed_path(self.settings, branch, write=True)
        if path.is_symlink() or not path.is_dir():
            raise ToolError("Worktrunk managed path is missing or is not a directory.")
        records = await worktree.list_registered(repo)
        selected = [row for row in records if row.path == path]
        if len(selected) != 1 or selected[0].unavailable or selected[0].branch != "refs/heads/" + branch:
            raise ToolError("Target is not an available registered Worktrunk worktree.")
        common = await worktree.common_directory(repo)
        if not _worktrees.bound_git_pointer(path, common):
            raise ToolError("Worktrunk .git pointer does not belong to the selected repository.")
        target = GitExecution(_worktrees.scoped_settings(self.settings, path))
        await _repository.verify_root(target)
        if await worktree.common_directory(target) != common:
            raise ToolError("Worktrunk checkout belongs to a different repository.")
        await _repository.require_writable_state(target)
        actual_branch, head, entries = await status.read_porcelain_v2(target)
        _, ref_head = await resolve_local_ref(repo, branch)
        if actual_branch != branch or head != valid_sha(expected_head) or head != ref_head or entries:
            raise ToolError("Worktrunk checkout is dirty or the observed branch HEAD changed.")
        return _worktrees.scoped_settings(self.settings, path)

    async def create(
        self,
        branch: str,
        base: str,
        expected_base_head: str,
        original_branch: str,
        original_head: str,
    ) -> dict[str, str]:
        _authorize(self.settings, write=True)

        repo = GitExecution(self.settings)
        fingerprint = await original_snapshot(self.settings, original_branch, original_head, allow_dirty=True)
        _, base_head = await resolve_local_ref(repo, base)
        if base_head != valid_sha(expected_base_head):
            raise ToolError("Base branch changed; no Worktrunk creation attempted.")
        path = _managed_path(self.settings, branch, write=True)
        if path.exists() or path.is_symlink():
            raise ToolError("Worktrunk path is occupied.")
        registered = await worktree.list_registered(repo)
        if any(r.branch == "refs/heads/" + branch or r.path == path for r in registered):
            raise ToolError("Worktrunk branch or path is already checked out.")
        result = await repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/" + branch,
            operation="Inspect Worktrunk branch",
            allow_failure=True,
        )
        if result.code != 1:
            raise ToolError("Worktrunk target branch already exists or cannot be inspected.")
        await worktree.require_ignored(repo, path.relative_to(repo.root).as_posix() + "/")
        await worktree.require_safe_checkout_tree(
            repo, self.settings.policy, path, base_head, operation="Inspect Worktrunk checkout paths"
        )
        # An explicit path prevents global Worktrunk configuration from placing files
        # outside the authorized workspace. This Worktrunk flag is experimental.
        await self._run(
            "switch",
            "--create",
            branch,
            "--base",
            base,
            "--path",
            str(path),
            "--no-cd",
            "--no-hooks",
            write=True,
        )
        await confirm_original(self.settings, original_branch, original_head, fingerprint, allow_dirty=True)
        records = await worktree.list_registered(repo)
        if not any(r.path == path and r.branch == "refs/heads/" + branch for r in records):
            raise ToolError("Worktrunk creation may have succeeded, but registration was not verified.")
        _, head = await resolve_local_ref(repo, branch)
        if head != base_head:
            raise ToolError("Worktrunk branch HEAD differs from the observed base.")
        return {"branch": branch, "head": head, "path": str(path.relative_to(repo.root))}

    async def remove(
        self, branch: str, expected_branch_head: str, *, delete_branch: bool | None = None
    ) -> dict[str, object]:
        """Delegate deletion decisions to Worktrunk after identifying an allowed checkout."""
        _authorize(self.settings, write=True)
        feature_name(branch)
        repo = GitExecution(self.settings)
        records = await worktree.list_registered(repo)
        matches = [row for row in records if row.branch == "refs/heads/" + branch and not row.unavailable]
        if len(matches) != 1:
            raise ToolError("Expected exactly one registered, available Worktrunk target.")
        path = matches[0].path
        if not path.is_absolute() or path == repo.root or not path.is_dir() or path.is_symlink():
            raise ToolError("Worktrunk removal target is not an available linked checkout.")
        if path.resolve() != path:
            raise ToolError("Worktrunk removal target traverses a symlink.")
        try:
            self.settings.policy.require_path_access(path, write=True)
        except PermissionError as error:
            raise ToolError(str(error)) from None
        _, actual_head = await resolve_local_ref(repo, branch)
        if actual_head != valid_sha(expected_branch_head):
            raise ToolError("Worktrunk cleanup branch changed; reobserve before removal.")

        args = ["remove", "--foreground", "--no-hooks"]
        if delete_branch is not None:
            args.append("--delete-branch" if delete_branch else "--no-delete-branch")
        # Absolute path binds the request to the checked registered worktree, not
        # to Worktrunk's default branch/path resolution or its current worktree.
        args.append(str(path))
        response = await self._run(*args, write=True)
        results = response if isinstance(response, list) else [response]
        if len(results) != 1 or not isinstance(results[0], dict):
            raise ToolError("Worktrunk removal result is ambiguous; inspect current state.")
        outcome = cast(dict[str, object], results[0])
        branch_outcome = outcome.get("branch_outcome")
        if (
            outcome.get("branch") != branch
            or outcome.get("path") != str(path)
            or branch_outcome
            not in {
                "deleted",
                "not_attempted",
                "retained_unmerged",
                "retained_checked_out",
                "retained_raced",
                "retained_failed",
            }
        ):
            raise ToolError("Worktrunk removal result did not identify the requested target.")
        return {
            "branch": branch,
            "path": str(path.relative_to(repo.root)),
            "worktree_removed": True,
            "branch_deleted": branch_outcome == "deleted",
            "branch_outcome": branch_outcome,
        }
