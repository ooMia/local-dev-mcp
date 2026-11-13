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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, cast

from fastmcp.exceptions import ToolError

from .._execution import GitExecution
from .._remote import feature_name, valid_sha
from .._worktrees import require_permission
from ..commands import rev_parse, status, symbolic_ref, worktree
from ..facades._refs import resolve_local_ref

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

_OUTPUT_LIMIT = 1_048_576


def _checkout_path(settings: ServerSettings, branch: str, *, write: bool, requested_path: str | None = None) -> Path:
    """Select an explicit allowed checkout path, or preserve the legacy default."""
    feature_name(branch)
    root = settings.policy.root
    if requested_path is None:
        path = root / ".lab" / "wt" / hashlib.sha256(branch.encode("ascii")).hexdigest()[:24]
    else:
        relative = PurePosixPath(requested_path)
        if (
            not requested_path
            or len(requested_path) > 1024
            or any(ord(char) < 32 or char == "\\" for char in requested_path)
            or relative.is_absolute()
            or PureWindowsPath(requested_path).drive
            or ".." in relative.parts
            or relative.as_posix() != requested_path
            or relative == PurePosixPath(".")
        ):
            raise ToolError("Worktrunk checkout path must be a workspace-relative POSIX path.")
        path = root.joinpath(*relative.parts)
    try:
        settings.policy.require_path_access(path, write=write)
    except PermissionError as error:
        raise ToolError(str(error)) from None
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ToolError("Worktrunk checkout path cannot traverse symlinks.")
        if candidate != path and candidate.exists() and not candidate.is_dir():
            raise ToolError("Worktrunk checkout parent is not a directory.")
    return path


def _authorize(settings: ServerSettings, *, write: bool) -> None:
    require_permission(settings, write=write, backend_commands=("wt",))


def require_worktrunk_access(settings: ServerSettings, *, write: bool) -> None:
    """Expose the adapter's fixed logical access policy to submodules."""
    _authorize(settings, write=write)


async def _registered_checkout_path(settings: ServerSettings, repo: GitExecution, branch: str) -> Path | None:
    """Return one authorized linked checkout; None means the branch is not checked out."""
    from .. import _worktrees

    records = await worktree.list_registered(repo)
    matches = [row for row in records if row.branch == "refs/heads/" + branch]
    if not matches:
        return None
    if len(matches) != 1 or matches[0].unavailable:
        raise ToolError("Target Worktrunk registration is ambiguous or unavailable.")
    path = matches[0].path
    if (
        not path.is_absolute()
        or path == repo.root
        or not path.is_relative_to(repo.root)
        or not path.is_dir()
        or path.is_symlink()
        or path.resolve() != path
    ):
        raise ToolError("Target Worktrunk checkout is not an authorized linked directory.")
    try:
        settings.policy.require_path_access(path, write=True)
    except PermissionError as error:
        raise ToolError(str(error)) from None
    # Ask Git which repository owns this checkout rather than interpreting the
    # Native worktree receipt, .git pointer, and backlink metadata ourselves.
    from ..facades import _repository

    target = GitExecution(_worktrees.scoped_settings(settings, path))
    await _repository.verify_root(target)
    if await worktree.common_directory(target) != await worktree.common_directory(repo):
        raise ToolError("Target Worktrunk checkout is not linked to the selected repository.")
    return path


async def _require_original_ref(settings: ServerSettings, branch: str, head: str) -> None:
    """Pin controller identity without inspecting or hashing unrelated dirty files."""
    from ..facades import _repository

    repo = GitExecution(settings)
    await _repository.verify_root(repo)
    await _repository.require_writable_state(repo)
    actual_ref = await symbolic_ref.head_reference(repo)
    actual_head = await rev_parse.head_commit(repo)
    if actual_ref != "refs/heads/" + branch or actual_head != valid_sha(head):
        raise ToolError("Original checkout branch or HEAD changed. Reobserve before proceeding.")


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

    async def inspect(self, branch: str) -> dict[str, object]:
        """Report Git-observed checkout state without Worktrunk CLI or Native receipts."""
        from datetime import UTC, datetime

        from .. import _worktrees
        from ..facades import _repository

        require_permission(self.settings)  # Git read access only; wt is not required.
        feature_name(branch)
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        probe = await repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/" + branch,
            operation="Inspect Worktrunk branch",
            allow_failure=True,
        )
        if probe.code not in (0, 1):
            raise ToolError("Cannot determine whether the requested branch exists.")
        branch_head = (await resolve_local_ref(repo, branch))[1] if probe.code == 0 else None

        def report(
            state: str,
            *,
            reason: str | None = None,
            path: str | None = None,
            head: str | None = None,
            clean: bool | None = None,
            registered: bool = False,
        ) -> dict[str, object]:
            return {
                "project": str(repo.root),
                "branch": branch,
                "state": state,
                "reason": reason,
                "path": path,
                "head": head,
                "branch_head": branch_head,
                "clean": clean,
                "registered": registered,
                "observed_at": datetime.now(UTC).isoformat(),
            }

        records = await worktree.list_registered(repo)
        matches = [record for record in records if record.branch == "refs/heads/" + branch]
        if not matches:
            return report("not_checked_out" if branch_head else "absent")
        if len(matches) != 1:
            return report("blocked", reason="ambiguous_registration", registered=True)
        record = matches[0]
        if record.unavailable:
            return report("blocked", reason="registration_unavailable", registered=True)
        if branch_head is None:
            return report("blocked", reason="branch_ref_missing", registered=True)
        path = record.path
        if not path.is_absolute() or path == repo.root or not path.is_relative_to(repo.root):
            return report("blocked", reason="outside_workspace", registered=True)
        if not path.is_dir() or path.is_symlink() or path.resolve() != path:
            return report("blocked", reason="invalid_path", registered=True)
        try:
            self.settings.policy.require_path_access(path)
        except PermissionError:
            return report("blocked", reason="access_denied", registered=True)
        target = GitExecution(_worktrees.scoped_settings(self.settings, path))
        try:
            await _repository.verify_root(target)
            if await worktree.common_directory(target) != await worktree.common_directory(repo):
                return report("blocked", reason="repository_mismatch", registered=True)
        except ToolError:
            return report("blocked", reason="repository_mismatch", registered=True)
        if record.head != branch_head:
            return report("blocked", reason="head_mismatch", registered=True)
        try:
            checked_branch, head, entries = await status.read_porcelain_v2(target)
            _, latest_head = await resolve_local_ref(repo, branch)
        except ToolError:
            return report("blocked", reason="status_unavailable", registered=True)
        if checked_branch != branch or head != branch_head or latest_head != head:
            return report("blocked", reason="head_mismatch", registered=True)
        return report(
            "dirty" if entries else "ready",
            path=path.relative_to(repo.root).as_posix(),
            head=head,
            clean=not entries,
            registered=True,
        )

    async def list_worktrees(self, *, include_branches: bool = False) -> dict[str, Any]:
        args = ("list", "--no-progressive", "--branches") if include_branches else ("list", "--no-progressive")
        data = await self._run(*args, write=False)
        if not isinstance(data, dict) or data.get("schema") != 2 or not isinstance(data.get("items"), list):
            raise ToolError("Unsupported Worktrunk list schema; expected schema 2.")
        return data

    async def preview_prune(self, min_age_days: int) -> list[dict[str, object]]:
        """Use Worktrunk's actual dry-run criteria, without granting cleanup execution."""
        if not 0 <= min_age_days <= 365:
            raise ToolError("Cleanup min_age_days must be between 0 and 365.")
        result = await self._run("step", "prune", "--dry-run", "--min-age", f"{min_age_days}d", write=False)
        if not isinstance(result, list) or len(result) > 128:
            raise ToolError("Worktrunk prune preview returned an unsupported candidate set.")
        if not all(isinstance(row, dict) for row in result):
            raise ToolError("Worktrunk prune preview returned an invalid candidate.")
        candidates = cast(list[dict[str, object]], result)
        if not all(
            isinstance(row.get("branch"), str)
            and isinstance(row.get("path"), str)
            and bool(row["branch"])
            and bool(row["path"])
            for row in candidates
        ):
            raise ToolError("Worktrunk prune preview returned an invalid candidate.")
        if len({cast(str, row["branch"]) for row in candidates}) != len(candidates) or len(
            {cast(str, row["path"]) for row in candidates}
        ) != len(candidates):
            raise ToolError("Worktrunk prune preview contains ambiguous duplicate candidates.")
        return candidates

    async def require_ready(self, branch: str, expected_head: str, *, write: bool = True) -> ServerSettings:
        """Bind an exact clean Worktrunk checkout, enforcing write grants only for writers."""
        if write:
            _authorize(self.settings, write=True)
        else:
            require_permission(self.settings)
        from .. import _worktrees
        from ..facades import _repository

        observed = await self.inspect(branch)
        if observed["state"] != "ready" or observed["head"] != valid_sha(expected_head):
            raise ToolError("Worktrunk checkout is dirty, unavailable or the observed branch HEAD changed.")
        relative = observed["path"]
        if not isinstance(relative, str):
            raise ToolError("Worktrunk inspection did not return an authorized checkout path.")
        path = self.settings.policy.root / relative
        try:
            self.settings.policy.require_path_access(path, write=write)
        except PermissionError as error:
            raise ToolError(str(error)) from None
        scoped = _worktrees.scoped_settings(self.settings, path)
        target = GitExecution(scoped)
        await _repository.require_writable_state(target)
        actual_branch, head, entries = await status.read_porcelain_v2(target)
        _, ref_head = await resolve_local_ref(GitExecution(self.settings), branch)
        if actual_branch != branch or head != valid_sha(expected_head) or head != ref_head or entries:
            raise ToolError("Worktrunk checkout changed during readiness verification.")
        return scoped

    async def create(
        self,
        branch: str,
        base: str,
        expected_base_head: str | None,
        original_branch: str,
        original_head: str,
        *,
        run_hooks: bool = False,
        expected_branch_head: str | None = None,
        worktree_path: str | None = None,
    ) -> dict[str, str]:
        _authorize(self.settings, write=True)

        feature_name(branch)
        repo = GitExecution(self.settings)
        requested = (
            _checkout_path(self.settings, branch, write=True, requested_path=worktree_path)
            if worktree_path is not None
            else None
        )
        await _require_original_ref(self.settings, original_branch, original_head)
        result = await repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/" + branch,
            operation="Inspect Worktrunk branch",
            allow_failure=True,
        )
        if result.code == 0:
            _, head = await resolve_local_ref(repo, branch)
            if expected_branch_head is not None and head != valid_sha(expected_branch_head):
                raise ToolError("Existing Worktrunk branch HEAD changed; reobserve before reuse.")
            existing = await _registered_checkout_path(self.settings, repo, branch)
            if existing is not None and requested is not None and existing != requested:
                raise ToolError("Existing Worktrunk checkout path differs from the requested path.")
            if existing is None:
                path = requested or _checkout_path(self.settings, branch, write=True)
                if path.exists() or path.is_symlink():
                    raise ToolError("Worktrunk path is occupied; no checkout attempted.")
                await worktree.require_ignored(repo, path.relative_to(repo.root).as_posix() + "/")
                await worktree.require_safe_checkout_tree(
                    repo, self.settings.policy, path, head, operation="Inspect existing Worktrunk branch paths"
                )
                argv = ["switch", branch, "--path", str(path), "--no-cd"]
                argv.append("--yes" if run_hooks else "--no-hooks")
                response = await self._run(*argv, write=True)
                if (
                    not isinstance(response, dict)
                    or response.get("action") != "created"
                    or response.get("created_branch") is not False
                    or response.get("branch") != branch
                    or response.get("path") != str(path)
                ):
                    raise ToolError("Worktrunk checkout result is uncertain; reobserve before retrying.")
                existing = await _registered_checkout_path(self.settings, repo, branch)
                if existing != path:
                    raise ToolError("Worktrunk checkout registration does not match the requested path.")
                action = "checked_out"
            else:
                action = "reused"
            if existing is None:
                raise ToolError("Worktrunk checkout is unavailable after preparation.")
            from .. import _worktrees

            target = GitExecution(_worktrees.scoped_settings(self.settings, existing))
            checked_branch, checked_head, _ = await status.read_porcelain_v2(target)
            await _require_original_ref(self.settings, original_branch, original_head)
            _, latest_head = await resolve_local_ref(repo, branch)
            if checked_branch != branch or checked_head != head or latest_head != head:
                raise ToolError("Existing Worktrunk branch or checkout HEAD moved during inspection.")
            return {"branch": branch, "head": head, "path": str(existing.relative_to(repo.root)), "action": action}
        if result.code != 1:
            raise ToolError("Worktrunk branch cannot be safely inspected.")
        if expected_branch_head is not None:
            raise ToolError("Expected existing Worktrunk branch is absent.")
        if expected_base_head is None:
            raise ToolError("Creating a branch requires an exact observed base HEAD.")
        _, base_head = await resolve_local_ref(repo, base)
        if base_head != valid_sha(expected_base_head):
            raise ToolError("Base branch changed; no Worktrunk creation attempted.")
        path = requested or _checkout_path(self.settings, branch, write=True)
        if path.exists() or path.is_symlink():
            raise ToolError("Worktrunk path is occupied.")
        registered = await worktree.list_registered(repo)
        if any(r.branch == "refs/heads/" + branch or r.path == path for r in registered):
            raise ToolError("Worktrunk branch or path is already checked out.")
        await worktree.require_ignored(repo, path.relative_to(repo.root).as_posix() + "/")
        await worktree.require_safe_checkout_tree(
            repo, self.settings.policy, path, base_head, operation="Inspect Worktrunk checkout paths"
        )
        # An explicit path prevents global Worktrunk configuration from placing files
        # outside the authorized workspace. This Worktrunk flag is experimental.
        argv = ["switch", "--create", branch, "--base", base, "--path", str(path), "--no-cd"]
        argv.append("--yes" if run_hooks else "--no-hooks")
        await self._run(*argv, write=True)
        await _require_original_ref(self.settings, original_branch, original_head)
        records = await worktree.list_registered(repo)
        if not any(r.path == path and r.branch == "refs/heads/" + branch for r in records):
            raise ToolError("Worktrunk creation may have succeeded, but registration was not verified.")
        _, head = await resolve_local_ref(repo, branch)
        if head != base_head:
            raise ToolError("Worktrunk branch HEAD differs from the observed base.")
        return {"branch": branch, "head": head, "path": str(path.relative_to(repo.root)), "action": "created"}

    async def remove(
        self, branch: str, expected_branch_head: str, *, delete_branch: bool | None = None, run_hooks: bool = False
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

        # Refuse dirty or moved checkouts before invoking the destructive CLI.
        # This narrows the race window; Worktrunk's non-force guards remain mandatory.
        ready = await self.require_ready(branch, expected_branch_head, write=True)
        if ready.policy.root != path:
            raise ToolError("Worktrunk removal checkout moved; reobserve before removal.")

        args = ["remove", "--foreground", "--yes" if run_hooks else "--no-hooks"]
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

        # CLI JSON is a receipt, not proof that Git and the filesystem agree.
        # On uncertainty require a fresh inspection; never report a confirmed removal.
        if path.exists() or path.is_symlink():
            raise ToolError("Worktrunk removal is unconfirmed: checkout path still exists.")
        registered = await worktree.list_registered(repo)
        if any(record.path == path for record in registered):
            raise ToolError("Worktrunk removal is unconfirmed: Git still registers the checkout.")
        ref = await repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/" + branch,
            operation="Verify Worktrunk branch after removal",
            allow_failure=True,
        )
        if ref.code not in (0, 1) or (branch_outcome == "deleted") != (ref.code == 1):
            raise ToolError("Worktrunk branch outcome is unconfirmed; reobserve Git state.")
        return {
            "branch": branch,
            "path": str(path.relative_to(repo.root)),
            "worktree_removed": True,
            "branch_deleted": branch_outcome == "deleted",
            "branch_outcome": branch_outcome,
        }
