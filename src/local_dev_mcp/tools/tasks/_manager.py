"""Server-local, bounded executions of configured project task recipes."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from secrets import token_urlsafe

from fastmcp.exceptions import ToolError
from shellous import Runner, sh

from ...quality.evidence import VerifiedQualityEvidence
from ...redaction import BoundedRedactedOutput, Redactor
from ...settings import JobDefinition, Policy
from ._response import TaskInfo, TaskResult, TaskState
from ._worktree import TaskWorktree

SHORT_WAIT_SECONDS = 1.0
RETENTION_SECONDS = 30 * 60
MAX_RESULTS = 128
MAX_REQUESTS = 4096
OUTPUT_BYTES = 65536
_TASK_LOG_CONTEXT: ContextVar[bool] = ContextVar("local_dev_task_logs", default=False)


class _TaskLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Library cancellation diagnostics may contain arguments. Suppress only
        # this execution context, without disabling concurrent Git diagnostics.
        return not _TASK_LOG_CONTEXT.get()


logging.getLogger("shellous").addFilter(_TaskLogFilter())


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class _Prepared:
    recipe: JobDefinition
    executable: Path | None
    reason: str | None


@dataclass
class _Execution:
    job_id: str
    request_id: str
    prepared: _Prepared
    targets: tuple[str, ...]
    all_tests: bool
    stdout: BoundedRedactedOutput
    stderr: BoundedRedactedOutput
    policy: Policy
    worktree: TaskWorktree | None = None
    worktree_verified: bool | None = None
    state: TaskState = "running"
    exit_code: int | None = None
    termination_confirmed: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    finished_clock: float | None = None
    may_have_changed_files: bool = False
    task: asyncio.Task[None] | None = None
    process: Runner | None = None


def _action(state: TaskState) -> str:
    if state == "running":
        return "Read this job again with get_project_task, or request cancel_project_task."
    if state == "succeeded":
        return "Inspect the reported output and selected scope; exit code 0 alone does not prove tests ran."
    if state == "failed":
        return "Inspect the bounded diagnostics, correct the cause, and use a new request_id for an intentional rerun."
    if state == "start_failed":
        return "Check the configured permission, executable and workspace; settings changes require server restart."
    if state == "cleanup_unconfirmed":
        return "Inspect local processes and files before another run; process termination was not confirmed."
    return "Inspect current files before rerunning with a new request_id; cancellation and timeout do not undo changes."


class TaskManager:
    def __init__(
        self, policy: Policy, recipes: tuple[JobDefinition, ...], environment: Mapping[str, str], redactor: Redactor
    ) -> None:
        self._policy = policy
        self._environment = dict(environment)
        self._environment_valid = all(
            name and "=" not in name and "\0" not in name and "\0" not in value
            for name, value in self._environment.items()
        )
        self._redactor = redactor
        self._prepared = {recipe.name: self._prepare(recipe) for recipe in recipes}
        if len(self._prepared) != len(recipes):
            raise ValueError("Duplicate project task names.")
        self._results: dict[str, _Execution] = {}
        self._quality_evidence: dict[str, tuple[VerifiedQualityEvidence, float]] = {}
        self._requests: dict[str, tuple[str, str]] = {}
        self._active: str | None = None
        self._closed = False
        self._submission = asyncio.Lock()

    @asynccontextmanager
    async def exclusive_git_cleanup(self) -> AsyncGenerator[None]:
        """Serialize destructive worktree removal against task submission."""
        async with self._submission:
            if self._closed or self._active is not None:
                raise ToolError(
                    "A task is running, unconfirmed, or the server is closing; "
                    "inspect project jobs before finalizing a worktree."
                )
            yield

    def _recipe_cwd(self, recipe: JobDefinition, policy: Policy) -> Path:
        """Resolve one registered recipe's directory against the actual execution root."""
        try:
            relative = PurePosixPath(recipe.cwd)
            if (
                not recipe.cwd
                or "\\" in recipe.cwd
                or relative.is_absolute()
                or PureWindowsPath(recipe.cwd).drive
                or ".." in relative.parts
            ):
                raise ValueError("Invalid recipe directory")
            cwd = (policy.root / Path(*relative.parts)).resolve()
            if not cwd.is_relative_to(policy.root) or not cwd.is_dir():
                raise ValueError("Recipe directory is missing or outside the execution root")
            policy.require_path_access(cwd, traverse=True)
            if policy is not self._policy:
                original = (self._policy.root / Path(*relative.parts)).resolve()
                self._policy.require_path_access(original, traverse=True)
            return cwd
        except OSError, PermissionError, ValueError:
            raise ToolError(
                "Recipe working directory is unavailable or outside the permitted workspace scope."
            ) from None

    def _prepare(self, recipe: JobDefinition) -> _Prepared:
        try:
            self._policy.require_project_access()
            self._policy.require_command(recipe.permission)
        except PermissionError:
            return _Prepared(recipe, None, "permission_denied")
        if not self._environment_valid:
            return _Prepared(recipe, None, "environment_invalid")
        try:
            self._recipe_cwd(recipe, self._policy)
        except ToolError:
            return _Prepared(recipe, None, "cwd_unavailable")
        try:
            command = recipe.argv[0]
            if "/" in command or "\\" in command:
                binary = Path(command)
                binary = Path(os.path.abspath(self._policy.root / binary))
            else:
                search = os.pathsep.join(
                    os.path.abspath(self._policy.root / entry)
                    for entry in self._environment.get("PATH", os.defpath).split(os.pathsep)
                )
                selected = shutil.which(command, path=search)
                if selected is None:
                    return _Prepared(recipe, None, "executable_unavailable")
                binary = Path(os.path.abspath(selected))
            # Interpreter symlinks carry venv identity. Bind the invocation path
            # without dereferencing it to the base Python executable.
            if not binary.is_file() or not os.access(binary, os.X_OK):
                return _Prepared(recipe, None, "executable_unavailable")
        except OSError, ValueError, IndexError:
            return _Prepared(recipe, None, "executable_unavailable")
        return _Prepared(recipe, binary, None)

    def catalogue(self) -> list[TaskInfo]:
        return [
            TaskInfo(
                name=item.recipe.name,
                kind=item.recipe.kind,
                level=item.recipe.level,
                permission=item.recipe.permission,
                ready=item.reason is None,
                reason=item.reason,
                next_action=(
                    "Start this configured task with start_project_task."
                    if item.reason is None
                    else (
                        "Correct the recipe cwd and restart the server."
                        if item.reason == "cwd_unavailable"
                        else "Correct the task permission, environment or executable and restart the server."
                    )
                ),
                timeout_seconds=item.recipe.timeout_seconds,
                default_targets=[self._redactor.sanitize(value) for value in item.recipe.default_targets],
                cwd=self._redactor.sanitize(item.recipe.cwd),
                targets_relative_to="recipe_cwd",
            )
            for item in self._prepared.values()
        ]

    def _selectors(self, values: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(values) <= 128 or sum(len(value.encode("utf-8")) for value in values) > 32768:
            raise ToolError("Provide 1..128 test selectors using at most 32 KiB in total.")
        selected: list[str] = []
        for value in values:
            if not value or len(value) > 4096 or any(ord(char) < 32 for char in value) or value.startswith(("-", "@")):
                raise ToolError("Use relative test paths or node IDs, without CLI options or control characters.")
            prefix, separator, suffix = value.partition("::")
            path = PurePosixPath(prefix)
            if (
                not prefix
                or "\\" in prefix
                or path.is_absolute()
                or PureWindowsPath(prefix).drive
                or ".." in path.parts
                or (separator and not suffix)
            ):
                raise ToolError("Test selector paths must stay within the selected workspace; use relative node IDs.")
            normalized = path.as_posix() + (separator + suffix if separator else "")
            if normalized.startswith(("-", "@")):
                normalized = "./" + normalized
            if normalized not in selected:
                selected.append(normalized)
        return tuple(selected)

    def _require_targets(
        self, targets: tuple[str, ...], *, recipe: JobDefinition, policy: Policy | None = None
    ) -> None:
        selected_policy = self._policy if policy is None else policy
        cwd = self._recipe_cwd(recipe, selected_policy)
        for value in targets:
            try:
                relative = value.partition("::")[0]
                target = (cwd / relative).resolve()
                if not target.is_relative_to(cwd):
                    raise ValueError
                # A runner may recurse through this target; navigation-only
                # ancestors cannot grant access to their other descendants.
                selected_policy.require_path_access(target)
                self._policy.require_path_access(target)
                if selected_policy is not self._policy:
                    original = (self._policy.root / recipe.cwd / relative).resolve()
                    self._policy.require_path_access(original)
            except OSError, ValueError, PermissionError:
                raise ToolError("Test selector path is unavailable or outside the permitted workspace scope.") from None

    def _selection(self, recipe: JobDefinition, targets: list[str] | None, all_tests: bool) -> tuple[str, ...]:
        if recipe.kind != "test":
            if targets is not None or all_tests:
                raise ToolError("Lint and format use fixed arguments; test selection fields are not accepted.")
            return ()
        if all_tests:
            if targets is not None:
                raise ToolError("Use either explicit targets or all_tests=true, never both.")
            return self._selectors(recipe.default_targets)
        if targets is None:
            raise ToolError("Select test targets explicitly, or set all_tests=true for the configured full scope.")
        return self._selectors(tuple(targets))

    def _prune(self) -> None:
        now = time.monotonic()
        terminal = sorted(
            (item for item in self._results.values() if item.finished_clock is not None),
            key=lambda item: item.finished_clock or 0,
        )
        for item in terminal:
            if item.job_id == self._active:
                continue
            if now - (item.finished_clock or now) >= RETENTION_SECONDS or len(self._results) > MAX_RESULTS:
                del self._results[item.job_id]
        for token, (proof, recorded_at) in tuple(self._quality_evidence.items()):
            if now - recorded_at >= RETENTION_SECONDS or any(job not in self._results for job in proof.job_ids):
                self._quality_evidence.pop(token, None)
        # Request fingerprints remain bounded tombstones, so an evicted result
        # cannot turn a lost-response retry into a second process invocation.

    def _lookup(self, job_id: str) -> _Execution:
        self._prune()
        result = self._results.get(job_id)
        if result is None:
            raise ToolError("Job is unknown or expired on this server. Inspect current files; no task was replayed.")
        return result

    def _snapshot(self, result: _Execution, *, include_output: bool = False) -> TaskResult:
        reasons = [
            f"{name}:{output.reason}"
            for name, output in (("stdout", result.stdout), ("stderr", result.stderr))
            if output.reason is not None
        ]
        snapshot = TaskResult(
            job_id=result.job_id,
            request_id=result.request_id,
            task=result.prepared.recipe.name,
            kind=result.prepared.recipe.kind,
            level=result.prepared.recipe.level,
            state=result.state,
            exit_code=result.exit_code,
            termination_confirmed=result.termination_confirmed,
            output_included=include_output,
            stdout=result.stdout.text if include_output else "",
            stderr=result.stderr.text if include_output else "",
            output_complete=result.stdout.complete and result.stderr.complete,
            output_reason=";".join(reasons) or None,
            started_at=result.started_at,
            finished_at=result.finished_at,
            targets=[self._redactor.sanitize(value) for value in result.targets],
            all_tests=result.all_tests,
            cwd=self._redactor.sanitize(result.prepared.recipe.cwd),
            targets_relative_to="recipe_cwd",
            next_action=_action(result.state)
            + (
                ""
                if include_output
                else " To inspect retained masked diagnostics, call get_project_task "
                "with this job_id and include_output=true."
            ),
            may_have_changed_files=result.may_have_changed_files,
        )
        if result.worktree is not None:
            context = result.worktree
            snapshot["worktree"] = {
                "backend": context.backend,
                "branch": context.branch,
                "head": context.head,
                "original_branch": context.original_branch,
                "original_head": context.original_head,
                "path": result.policy.root.relative_to(self._policy.root).as_posix(),
                "verified": result.worktree_verified,
                "verification_error": (
                    "Git state verification did not succeed; inspect both worktrees before proceeding."
                    if result.worktree_verified is False
                    else None
                ),
            }
            if result.worktree_verified is False:
                snapshot["next_action"] = (
                    "Inspect original and target Git state; process exit does not verify this worktree."
                )
        return snapshot

    async def start(
        self,
        task: str,
        request_id: str,
        targets: list[str] | None = None,
        all_tests: bool = False,
        *,
        worktree: TaskWorktree | None = None,
    ) -> TaskResult:
        async with self._submission:
            result, created = await self._submit(task, request_id, targets, all_tests, worktree)
        if created and result.task is not None:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(result.task), timeout=SHORT_WAIT_SECONDS)
        return self._snapshot(result)

    async def _submit(
        self,
        task: str,
        request_id: str,
        targets: list[str] | None,
        all_tests: bool,
        worktree: TaskWorktree | None,
    ) -> tuple[_Execution, bool]:
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id) is None:
            raise ToolError("request_id must be 1..128 ASCII letters, digits, dots, underscores, colons or hyphens.")
        prepared = self._prepared.get(task)
        if prepared is None:
            raise ToolError("Unknown project task. Use list_project_tasks and select a configured name.")
        selection = self._selection(prepared.recipe, targets, all_tests)
        fingerprint = hashlib.sha256(
            json.dumps(
                [task, selection, all_tests, worktree.identity if worktree else None], ensure_ascii=True
            ).encode()
        ).hexdigest()
        self._prune()
        previous = self._requests.get(request_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ToolError("This request_id already names different task arguments; use a new request_id.")
            return self._lookup(previous[1]), False
        if self._closed:
            raise ToolError("This task manager is closing. Reconnect to a running server before starting work.")
        if len(self._requests) >= MAX_REQUESTS:
            raise ToolError("The request ledger is full. Inspect work and restart the server before new starts.")
        if self._active is not None:
            raise ToolError(
                f"Another project task is active (active_job_id={self._active}). "
                "Use get_project_task or cancel_project_task with that job_id; "
                "if cleanup is unconfirmed, inspect local processes and restart before new work."
            )
        if prepared.reason == "permission_denied":
            raise ToolError(
                "Task is not ready (permission_denied). Allow its exact logical permission in "
                "system.allowed_commands and restart the server."
            )
        if prepared.reason == "environment_invalid":
            raise ToolError(
                "Task is not ready (environment_invalid). Correct invalid names or NUL-containing values "
                "in its environment source and restart the server."
            )
        if prepared.reason == "cwd_unavailable":
            raise ToolError(
                "Task is not ready (cwd_unavailable). Check the recipe working directory, access policy "
                "and symlinks, then restart the server."
            )
        if prepared.reason is not None:
            raise ToolError(
                "Task is not ready (executable_unavailable). Check its installation and configured executable path, "
                "then restart the server."
            )
        if worktree is not None and prepared.recipe.kind == "format":
            raise ToolError("Managed worktree checks do not run modifying format recipes.")
        policy = self._policy if worktree is None else await worktree.prepare()
        if self._closed:
            raise ToolError("This task manager closed during preparation; no process was submitted.")
        self._require_targets(selection, recipe=prepared.recipe, policy=policy)
        job_id = token_urlsafe(24)
        result = _Execution(
            job_id,
            request_id,
            prepared,
            selection,
            all_tests,
            BoundedRedactedOutput(self._redactor, limit=OUTPUT_BYTES),
            BoundedRedactedOutput(self._redactor, limit=OUTPUT_BYTES),
            policy=policy,
            worktree=worktree,
        )
        self._results[job_id] = result
        self._requests[request_id] = (fingerprint, job_id)
        self._active = job_id
        result.task = asyncio.create_task(self._execute(result), name="local-dev-mcp-project-task")
        return result, True

    def store_quality_evidence(self, proof: VerifiedQualityEvidence) -> str:
        """Retain only bounded, server-local evidence tied to retained job IDs."""
        self._prune()
        if any(job not in self._results for job in proof.job_ids):
            raise ToolError("A quality job expired before verification.")
        if len(self._quality_evidence) >= MAX_RESULTS:
            raise ToolError("Quality evidence ledger is full; no proof was issued.")
        token = token_urlsafe(24)
        self._quality_evidence[token] = (proof, time.monotonic())
        return token

    def get_quality_evidence(self, token: str) -> VerifiedQualityEvidence:
        self._prune()
        record = self._quality_evidence.get(token)
        if record is None:
            raise ToolError("Quality evidence is unknown or expired on this server.")
        return record[0]

    def get(self, job_id: str, *, include_output: bool = False) -> TaskResult:
        return self._snapshot(self._lookup(job_id), include_output=include_output)

    async def wait(self, job_id: str, *, include_output: bool = False) -> TaskResult:
        """Wait for one retained job to reach a terminal state without cancelling it."""
        result = self._lookup(job_id)
        if result.task is not None:
            await asyncio.shield(result.task)
        return self._snapshot(result, include_output=include_output)

    async def cancel(self, job_id: str) -> TaskResult:
        result = self._lookup(job_id)
        if result.state == "running" and result.task is not None:
            result.task.cancel()
            try:
                await asyncio.shield(result.task)
            except asyncio.CancelledError:
                if not result.task.done():
                    raise
                self._cancel_unstarted(result)
        return self._snapshot(result)

    def _cancel_unstarted(self, result: _Execution) -> None:
        if result.state == "running":
            result.state = "cancelled"
            result.termination_confirmed = result.process is None
            self._finish(result)

    async def close(self) -> None:
        self._closed = True
        active = [item for item in self._results.values() if item.state == "running" and item.task is not None]
        pending: list[asyncio.Task[None]] = []
        for item in active:
            if item.task is not None:
                item.task.cancel()
                pending.append(item.task)
        if pending:
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        for item in active:
            self._cancel_unstarted(item)

    async def _drain(self, stream: asyncio.StreamReader | None, output: BoundedRedactedOutput) -> None:
        if stream is None:
            raise RuntimeError("Task output stream is unavailable.")
        while chunk := await stream.read(65536):
            output.feed(chunk)

    def _finish(self, result: _Execution) -> None:
        result.stdout.finish()
        result.stderr.finish()
        result.finished_at = _now()
        result.finished_clock = time.monotonic()
        if self._active == result.job_id and result.termination_confirmed:
            self._active = None
        self._prune()

    async def _execute(self, result: _Execution) -> None:
        token = _TASK_LOG_CONTEXT.set(True)
        result.started_at = _now()
        try:
            if result.worktree is not None and await result.worktree.prepare() != result.policy:
                raise ToolError("Managed worktree execution policy changed before process start.")
            result.policy.require_project_access()
            result.policy.require_command(result.prepared.recipe.permission)
            self._require_targets(result.targets, recipe=result.prepared.recipe, policy=result.policy)
            binary = result.prepared.executable
            if binary is None:
                raise FileNotFoundError
            command = (
                sh.result(str(binary), *result.prepared.recipe.argv[1:], *result.targets)
                .set(
                    cwd=self._recipe_cwd(result.prepared.recipe, result.policy),
                    env=self._environment,
                    inherit_env=False,
                    timeout=result.prepared.recipe.timeout_seconds,
                    alt_name="project task",
                )
                .stdin(sh.DEVNULL)
                .stdout(sh.CAPTURE)
                .stderr(sh.CAPTURE)
            )
            # Normal lint/test programs can still create caches or other files.
            result.may_have_changed_files = True
            async with command as process:
                result.process = process
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._drain(process.stdout, result.stdout))
                    group.create_task(self._drain(process.stderr, result.stderr))
            result.exit_code = process.returncode
            result.termination_confirmed = process.returncode is not None
            if not result.termination_confirmed:
                result.state = "cleanup_unconfirmed"
            elif process.cancelled:
                result.state = "cancelled"
            else:
                result.state = "succeeded" if process.returncode == 0 else "failed"
        except TimeoutError:
            self._interrupted(result, "timed_out")
        except asyncio.CancelledError:
            self._interrupted(result, "cancelled")
        except OSError, PermissionError, ToolError, ValueError:
            if result.process is None:
                result.state = "start_failed"
                result.termination_confirmed = True
                result.may_have_changed_files = False
            else:
                self._interrupted(result, "failed")
        except Exception:
            self._interrupted(result, "failed")
        finally:
            try:
                if result.worktree is not None:
                    final_state = result.state
                    result.state = "running"
                    try:
                        await result.worktree.verify()
                        result.worktree_verified = True
                    except asyncio.CancelledError:
                        result.worktree_verified = False
                        if final_state == "succeeded":
                            final_state = "cancelled"
                    except Exception:
                        result.worktree_verified = False
                        if final_state == "succeeded":
                            final_state = "failed"
                    finally:
                        result.state = final_state
            finally:
                self._finish(result)
                _TASK_LOG_CONTEXT.reset(token)

    def _interrupted(self, result: _Execution, state: TaskState) -> None:
        process = result.process
        result.exit_code = process.returncode if process is not None else None
        result.termination_confirmed = (process is not None and process.returncode is not None) or (
            process is None and not result.may_have_changed_files
        )
        result.state = state if result.termination_confirmed else "cleanup_unconfirmed"
