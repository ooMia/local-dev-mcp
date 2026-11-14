"""Project-root Trail structural index checks and explicit, bounded refreshes.

No workspace selectors or generic command execution: target is always Policy.root.
The Trail command is opt-in through the existing logical 'trail' permission.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from ...settings import Policy

type StructuralState = Literal["fresh", "stale", "missing", "unknown", "unavailable"]
type DeepState = Literal["fresh", "stale", "unknown", "unavailable"]


class IndexReport(TypedDict):
    project: str
    provider: Literal["trail"]
    structural_state: StructuralState
    deep_state: DeepState
    ready_for_structural_search: bool
    pending_changes: bool
    refresh_performed: bool
    outcome: Literal["checked", "already_fresh", "updated", "failed", "unavailable"]
    reason: str | None
    observed_at: str


@dataclass(frozen=True)
class TrailProcessResult:
    exit_code: int | None
    output: str
    error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _state(output: str, label: str) -> str | None:
    match = re.search(rf"(?mi)^{re.escape(label)}:\s*(OK|FRESH|CURRENT|STALE|MISSING)\b", output)
    return match.group(1).lower() if match is not None else None


def structural_state(run: TrailProcessResult) -> StructuralState:
    """Determine wiring freshness independently of optional Deep summaries."""
    if run.error is not None:
        return "unknown"
    if re.search(r"(?m)^graph check: NO GRAPH\b", run.output):
        return "missing"
    state = _state(run.output, "graph check")
    if state in ("ok", "fresh", "current"):
        return "fresh"
    if state == "stale":
        # graph check: STALE includes Deep-only stale summaries, which do not
        # invalidate a freshly built, deterministic wiring graph.
        tail = run.output.split("graph check: STALE", 1)[1]
        structural_drift = [re.search(rf"(?m)^{name} \(([0-9]+)\):", tail) for name in ("changed", "added", "removed")]
        if any(match is not None and int(match.group(1)) > 0 for match in structural_drift):
            return "stale"
        if re.search(r"(?m)^stale summaries \([0-9]+\):", tail):
            return "fresh"
        return "unknown"
    if re.search(r"(?m)^trail build --check: NO GRAPH\b", run.output):
        return "missing"
    return "unknown"


def deep_state(run: TrailProcessResult) -> DeepState:
    """Interpret concept drift and stale symbol summaries as Deep-only signals."""
    if run.error is not None:
        return "unknown"
    if re.search(r"(?m)^deep layer: not built\b", run.output):
        return "unavailable"
    state = _state(run.output, "trail build --check")
    if state == "stale" or re.search(r"(?m)^stale summaries \([0-9]+\):", run.output):
        return "stale"
    if state in ("ok", "fresh", "current"):
        return "fresh"
    return "unknown"


class IndexManager:
    """Index only the configured project, never a caller-supplied filesystem path."""

    def __init__(
        self,
        policy: Policy,
        environment: Mapping[str, str],
        *,
        executable: str | None = None,
        refresh_on_write: bool = False,
    ) -> None:
        self.policy = policy
        self.environment = dict(environment)
        self.executable = executable
        self.refresh_on_write = refresh_on_write
        self._lock = asyncio.Lock()
        self._revision = 0
        self._pending = False
        self._auto_task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_reason: str | None = None

    def _binary(self) -> Path | None:
        try:
            self.policy.require_project_access()
            self.policy.require_command("trail")
        except PermissionError:
            return None
        selected: str | None
        if self.executable is not None:
            candidate = Path(self.executable)
            if not candidate.is_absolute() or candidate.name not in ("trail", "trail.exe"):
                return None
            selected = str(candidate)
        else:
            selected = shutil.which("trail", path=self.environment.get("PATH", os.defpath))
        if selected is None:
            return None
        path = Path(selected)
        try:
            return path if path.is_file() and os.access(path, os.X_OK) else None
        except OSError:
            return None

    async def _run(self, binary: Path, arguments: tuple[str, ...], timeout: float) -> TrailProcessResult:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(binary),
                *arguments,
                cwd=str(self.policy.root),
                env=self.environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async def collect() -> bytes:
                assert process is not None and process.stdout is not None
                data = bytearray()
                while chunk := await process.stdout.read(8192):
                    if len(data) + len(chunk) > 128 * 1024:
                        raise ValueError("output_limit")
                    data.extend(chunk)
                await process.wait()
                return bytes(data)

            output = await asyncio.wait_for(collect(), timeout)
            return TrailProcessResult(process.returncode, output.decode("utf-8", errors="replace"))
        except TimeoutError:
            return TrailProcessResult(None, "", "timeout")
        except ValueError:
            return TrailProcessResult(None, "", "output_limit")
        except OSError:
            return TrailProcessResult(None, "", "execution_unavailable")
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    def _report(
        self,
        structural: StructuralState,
        deep: DeepState,
        outcome: Literal["checked", "already_fresh", "updated", "failed", "unavailable"],
        *,
        performed: bool = False,
        reason: str | None = None,
    ) -> IndexReport:
        return {
            "project": str(self.policy.root),
            "provider": "trail",
            "structural_state": structural,
            "deep_state": deep,
            "ready_for_structural_search": structural == "fresh" and not self._pending,
            "pending_changes": self._pending,
            "refresh_performed": performed,
            "outcome": outcome,
            "reason": reason,
            "observed_at": _now(),
        }

    async def _check(self, binary: Path) -> tuple[StructuralState, DeepState, str | None]:
        check = await self._run(binary, ("build", "--check", str(self.policy.root)), 30)
        return (
            structural_state(check),
            deep_state(check),
            check.error or ("check_unrecognized" if structural_state(check) == "unknown" else None),
        )

    async def status(self) -> IndexReport:
        binary = self._binary()
        if binary is None:
            return self._report("unavailable", "unavailable", "unavailable", reason="trail_not_configured")
        revision = self._revision
        structural, deep, error = await self._check(binary)
        if structural == "fresh" and not self._lock.locked() and revision == self._revision:
            self._pending = False
        return self._report(structural, deep, "checked", reason=error or self._last_reason)

    async def refresh(self) -> IndexReport:
        binary = self._binary()
        if binary is None:
            return self._report("unavailable", "unavailable", "unavailable", reason="trail_not_configured")
        async with self._lock:
            revision = self._revision
            structural, deep, error = await self._check(binary)
            if structural == "fresh":
                if self._revision == revision:
                    self._pending = False
                self._last_reason = None
                return self._report(structural, deep, "already_fresh")
            if structural == "unknown":
                self._last_reason = error
                return self._report(structural, deep, "failed", reason=error)
            result = await self._run(binary, ("build", str(self.policy.root)), 120)
            if result.exit_code != 0 or result.error is not None:
                self._last_reason = result.error or "build_nonzero"
                return self._report("unknown", deep, "failed", performed=True, reason=self._last_reason)
            structural, deep, error = await self._check(binary)
            if structural in ("stale", "missing"):
                # Some Trail caches can replay obsolete extraction even after
                # an incremental success. Force one deterministic reparse only
                # when the post-build graph still disagrees with the source.
                retry = await self._run(binary, ("build", "--no-reuse", str(self.policy.root)), 120)
                if retry.exit_code != 0 or retry.error is not None:
                    self._last_reason = retry.error or "full_build_nonzero"
                    return self._report("unknown", deep, "failed", performed=True, reason=self._last_reason)
                structural, deep, error = await self._check(binary)
            if structural == "fresh" and self._revision == revision:
                self._pending = False
            self._last_reason = error
            return self._report(
                structural,
                deep,
                "updated" if structural == "fresh" else "failed",
                performed=True,
                reason=error,
            )

    def changed(self) -> None:
        """Enqueue an optional coalesced refresh without altering the file-write receipt."""
        self._revision += 1
        self._pending = True
        if not self.refresh_on_write or self._closed or self._binary() is None:
            return
        if self._auto_task is None or self._auto_task.done():
            self._auto_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(0.25)
                revision = self._revision
                report = await self.refresh()
                if report["outcome"] not in ("already_fresh", "updated") or revision == self._revision:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            self._last_reason = "automatic_refresh_failed"

    async def wait_for_pending_refresh(self) -> None:
        """Wait for this instance's current automatic refresh; does not start a job."""
        if self._auto_task is not None:
            await self._auto_task

    async def close(self) -> None:
        self._closed = True
        if self._auto_task is not None and not self._auto_task.done():
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
