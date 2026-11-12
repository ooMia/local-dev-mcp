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
from typing import TYPE_CHECKING, Any, Literal, cast

from fastmcp.exceptions import ToolError

from ....settings import protected_name
from .._execution import GitExecution, text_output
from .._remote import feature_name, valid_sha
from .._worktrees import confirm_original, original_snapshot
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
    try:
        settings.policy.require_project_access()
        settings.policy.require_command("git")
        settings.policy.require_command("wt")
        if write:
            settings.policy.require_command("git.worktree.write")
    except PermissionError as error:
        raise ToolError(str(error)) from None


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

    async def switch_main_checkout(self, path: Path) -> None:
        """Execute only the fixed main checkout command; path checks belong to main.py."""
        await self._run("switch", "main", "--path", str(path), "--no-cd", "--no-hooks", write=True)

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
        tree = await repo.run(
            "ls-tree", "-r", "--name-only", "-z", base_head, operation="Inspect Worktrunk checkout paths"
        )
        for name in filter(None, text_output(tree.output).split("\0")):
            if any(protected_name(part) for part in name.split("/")):
                raise ToolError("Target Worktrunk tree contains protected names.")
            if not self.settings.policy.allows_path(path / name) or not self.settings.policy.allows_path(
                repo.root / name
            ):
                raise ToolError("Target Worktrunk tree extends outside the authorized workspace.")
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

    async def merge(
        self,
        branch: str,
        expected_source_head: str,
        expected_main_head: str,
        mode: Literal["squash", "ff"] = "squash",
    ) -> dict[str, str]:
        _authorize(self.settings, write=True)
        repo = GitExecution(self.settings)
        path = _managed_path(self.settings, branch, write=True)
        records = await worktree.list_registered(repo)
        if not any(r.path == path and r.branch == "refs/heads/" + branch for r in records):
            raise ToolError("Only a registered managed Worktrunk worktree can be merged.")
        _, source_head = await resolve_local_ref(repo, branch)
        _, main_head = await resolve_local_ref(repo, "main")
        if source_head != valid_sha(expected_source_head) or main_head != valid_sha(expected_main_head):
            raise ToolError("Source or main changed; no merge attempted.")
        from .._worktrees import scoped_settings

        _, target_head, target_entries = await status.read_porcelain_v2(
            GitExecution(scoped_settings(self.settings, path))
        )
        if target_head != source_head or target_entries:
            raise ToolError("Source Worktrunk worktree is dirty or moved.")
        # Require an actual clean main worktree. Avoid implicit creation of a target
        # checkout or overwriting another agent's uncommitted work.
        main_rows = [r for r in records if r.branch == "refs/heads/main"]
        if len(main_rows) != 1 or main_rows[0].unavailable:
            raise ToolError("A registered clean main worktree is required.")
        try:
            self.settings.policy.require_path_access(main_rows[0].path, write=True)
        except PermissionError as error:
            raise ToolError(str(error)) from None
        main_settings = scoped_settings(self.settings, main_rows[0].path)
        _, current_main, main_entries = await status.read_porcelain_v2(GitExecution(main_settings))
        if current_main != main_head or main_entries:
            raise ToolError("Main worktree is dirty or moved.")
        check = await repo.run(
            "merge-base",
            "--is-ancestor",
            main_head,
            source_head,
            operation="Check Worktrunk ancestry",
            allow_failure=True,
        )
        if check.code != 0:
            raise ToolError("Main is not an ancestor of the feature; reconcile manually.")
        await self._run(
            "merge",
            "--branch",
            branch,
            "--no-rebase",
            "--no-remove",
            "--stage",
            "none",
            "--no-hooks",
            *(["--no-squash"] if mode == "ff" else []),
            "main",
            write=True,
        )
        _, updated_main = await resolve_local_ref(repo, "main")
        if updated_main == main_head:
            raise ToolError("Worktrunk returned success but main was not updated.")
        if mode == "ff" and updated_main != source_head:
            raise ToolError("Worktrunk did not fast-forward main to the observed source HEAD.")
        source_tree = (
            await repo.run("rev-parse", source_head + "^{tree}", operation="Read source tree")
        ).output.strip()
        main_tree = (
            await repo.run("rev-parse", updated_main + "^{tree}", operation="Read integrated tree")
        ).output.strip()
        if source_tree != main_tree:
            raise ToolError("Integrated main tree differs from the source; inspect before retrying.")
        _, source_after = await resolve_local_ref(repo, branch)
        return {
            "branch": branch,
            "source_after": source_after,
            "main_before": main_head,
            "main_after": updated_main,
            "mode": mode,
        }

    async def remove(self, branch: str, expected_branch_head: str, *, delete_branch: bool = False) -> dict[str, object]:
        _authorize(self.settings, write=True)
        repo = GitExecution(self.settings)
        path = _managed_path(self.settings, branch, write=True)
        records = await worktree.list_registered(repo)
        if not any(r.path == path and r.branch == "refs/heads/" + branch for r in records):
            raise ToolError("Only a registered managed Worktrunk worktree can be removed.")
        _, actual_head = await resolve_local_ref(repo, branch)
        if actual_head != valid_sha(expected_branch_head):
            raise ToolError("Worktrunk cleanup branch changed; reobserve before removal.")
        from .._worktrees import scoped_settings

        target = GitExecution(scoped_settings(self.settings, path))
        actual_branch, checkout_head, entries = await status.read_porcelain_v2(target)
        if actual_branch != branch or checkout_head != actual_head or entries:
            raise ToolError("Refuse to remove a changed or dirty Worktrunk worktree.")
        ignored = await target.run(
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            operation="Inspect ignored worktree content",
        )
        if ignored.output:
            raise ToolError("Ignored files remain in Worktrunk worktree; refuse cleanup.")
        # Worktrunk remove emits a JSON array on some supported versions.
        await self._run(
            "remove",
            "--foreground",
            "--no-hooks",
            "--delete-branch" if delete_branch else "--no-delete-branch",
            branch,
            write=True,
        )
        after = await worktree.list_registered(repo)
        if path.exists() or any(r.path == path for r in after):
            raise ToolError("Worktrunk remove result could not be verified.")
        found = await repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/" + branch,
            operation="Verify Worktrunk branch deletion",
            allow_failure=True,
        )
        expected_code = 1 if delete_branch else 0
        if found.code != expected_code:
            raise ToolError("Worktrunk branch cleanup outcome was not verified.")
        return {"branch": branch, "worktree_removed": True, "branch_deleted": delete_branch}
