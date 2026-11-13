"""Cleanup preview never removes registered worktrees or publishes branch names outside policy."""

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


def _candidates(data: dict[str, object]) -> list[dict[str, object]]:
    result = data["candidates"]
    assert isinstance(result, list)
    return cast(list[dict[str, object]], result)


def test_cleanup_plan_needs_explicit_worktrunk_access(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git",)))) as client:
            result = await success(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
            assert result["write_requested"] is False
            assert result["complete"] is False
            assert result["reason"] == "worktrunk_not_authorized"
            assert _candidates(result) == []
            await failure(client, "plan_project_worktree_cleanup", {"min_age_days": -1})
            await failure(client, "plan_project_worktree_cleanup", {"min_age_days": 366})
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_cleanup_plan_filters_dirty_unmerged_and_outside_candidates(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        clean = tmp_path / ".lab" / "clean"
        dirty = tmp_path / ".lab" / "dirty"
        unmerged = tmp_path / ".lab" / "unmerged"
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        await git(tmp_path, "worktree", "add", str(clean), "feat/publish")
        await git(tmp_path, "worktree", "add", "-b", "feat/dirty", str(dirty), "main")
        await git(tmp_path, "worktree", "add", "-b", "feat/unmerged", str(unmerged), "main")
        await git(tmp_path, "worktree", "add", "-b", "feat/outside", str(outside), "main")
        (dirty / "base.txt").write_text("unsaved user content")
        (unmerged / "feature.txt").write_text("new feature")
        await git(unmerged, "add", "feature.txt")
        await git(unmerged, "commit", "-m", "feat: not merged")
        before = await git(tmp_path, "worktree", "list", "--porcelain")

        async def preview(self: WorktrunkAdapter, min_age_days: int) -> list[dict[str, object]]:
            assert min_age_days == 0
            return [
                {"branch": "feat/publish", "path": str(clean), "branch_deleted": True, "reason": "merged"},
                {"branch": "feat/dirty", "path": str(dirty)},
                {"branch": "feat/unmerged", "path": str(unmerged)},
                {"branch": "feat/outside", "path": str(outside)},
                {"branch": "main", "path": str(tmp_path)},
            ]

        monkeypatch.setattr(WorktrunkAdapter, "preview_prune", preview)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            result = await success(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
            assert result["complete"] is True
            assert result["write_requested"] is False
            assert result["omitted_count"] == 4
            rows = _candidates(result)
            assert len(rows) == 1
            assert rows[0]["branch"] == "feat/publish"
            assert rows[0]["head"] == head
            assert rows[0]["path"] == ".lab/clean"
            assert rows[0]["branch_deleted_by_worktrunk"] is True
            assert "feat/outside" not in str(result)
            assert str(outside) not in str(result)
        assert await git(tmp_path, "worktree", "list", "--porcelain") == before
        assert (dirty / "base.txt").read_text() == "unsaved user content"
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_cleanup_plan_rejects_mismatched_dry_run_paths(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        clean = tmp_path / ".lab" / "clean"
        other = tmp_path / ".lab" / "other"
        await git(tmp_path, "worktree", "add", str(clean), "feat/publish")
        await git(tmp_path, "worktree", "add", "-b", "feat/other", str(other), "main")
        before = await git(tmp_path, "worktree", "list", "--porcelain")

        async def preview(self: WorktrunkAdapter, min_age_days: int) -> list[dict[str, object]]:
            return [{"branch": "feat/publish", "path": str(other), "branch_deleted": True}]

        monkeypatch.setattr(WorktrunkAdapter, "preview_prune", preview)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            result = await success(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
            assert result["complete"] is True
            assert result["omitted_count"] == 1
            assert _candidates(result) == []
            assert str(other) not in str(result)
        assert await git(tmp_path, "worktree", "list", "--porcelain") == before
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_cleanup_plan_rejects_invalid_cli_candidate_schema(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)

        async def invalid_cli(self: WorktrunkAdapter, *argv: str, write: bool) -> list[dict[str, object]]:
            assert not write
            return [{"branch": "feat/publish"}]

        monkeypatch.setattr(WorktrunkAdapter, "_run", invalid_cli)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            result = await success(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
            assert result["complete"] is False
            assert result["reason"] == "prune_preview_unavailable"
            assert _candidates(result) == []
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_cleanup_plan_real_cli_dry_run_does_not_modify_git_state(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    if shutil.which("wt") is None:
        pytest.skip("Worktrunk CLI is unavailable on this host")

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        target = tmp_path / ".lab" / "prune"
        await git(tmp_path, "worktree", "add", str(target), "feat/publish")
        before = await git(tmp_path, "worktree", "list", "--porcelain")
        proc = await asyncio.create_subprocess_exec(
            "wt",
            "-C",
            str(tmp_path),
            "step",
            "prune",
            "--dry-run",
            "--min-age",
            "0d",
            "--format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        data, diagnostic = await proc.communicate()
        assert proc.returncode == 0, (proc.returncode, diagnostic[:200])
        assert b'"branch": "feat/publish"' in data
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            result = await success(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
            assert result["write_requested"] is False
            assert result["target_head"] == head
            assert result["complete"] is True, (result["reason"], result["candidates"])
            rows = _candidates(result)
            assert len(rows) == 1 and rows[0]["branch"] == "feat/publish"
        assert target.exists()
        assert await git(tmp_path, "worktree", "list", "--porcelain") == before
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_cleanup_plan_rejects_main_move_during_final_checkout_check(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        target = tmp_path / ".lab" / "clean"
        await git(tmp_path, "worktree", "add", str(target), "feat/publish")

        async def preview(self: WorktrunkAdapter, min_age_days: int) -> list[dict[str, object]]:
            return [{"branch": "feat/publish", "path": str(target)}]

        inspect_original = WorktrunkAdapter.inspect
        inspections = 0

        async def inspect(self: WorktrunkAdapter, branch: str) -> dict[str, object]:
            nonlocal inspections
            result = await inspect_original(self, branch)
            inspections += 1
            if inspections == 2:
                await git(tmp_path, "commit", "--allow-empty", "-m", "chore: advance main")
            return result

        monkeypatch.setattr(WorktrunkAdapter, "preview_prune", preview)
        monkeypatch.setattr(WorktrunkAdapter, "inspect", inspect)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            await failure(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
        assert inspections == 2
        assert await git(tmp_path, "rev-parse", "HEAD") != head
        assert await git(target, "rev-parse", "HEAD") == head
        assert target.exists()

    asyncio.run(scenario())


def test_cleanup_plan_rejects_checkout_dirty_between_inspections(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        target = tmp_path / ".lab" / "clean"
        await git(tmp_path, "worktree", "add", str(target), "feat/publish")

        async def preview(self: WorktrunkAdapter, min_age_days: int) -> list[dict[str, object]]:
            return [{"branch": "feat/publish", "path": str(target)}]

        inspect_original = WorktrunkAdapter.inspect
        inspections = 0

        async def inspect(self: WorktrunkAdapter, branch: str) -> dict[str, object]:
            nonlocal inspections
            inspections += 1
            if inspections == 2:
                (target / "base.txt").write_text("concurrent user edit\n")
            return await inspect_original(self, branch)

        monkeypatch.setattr(WorktrunkAdapter, "preview_prune", preview)
        monkeypatch.setattr(WorktrunkAdapter, "inspect", inspect)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            await failure(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
        assert inspections == 2
        assert (target / "base.txt").read_text() == "concurrent user edit\n"
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert await git(target, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


@pytest.mark.parametrize("duplicate_field", ["branch", "path"])
def test_cleanup_plan_rejects_duplicate_cli_candidates(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, duplicate_field: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)

        async def duplicate_cli(self: WorktrunkAdapter, *argv: str, write: bool) -> list[dict[str, object]]:
            assert not write
            rows: list[dict[str, object]] = [
                {"branch": "feat/publish", "path": str(tmp_path / ".lab" / "first")},
                {"branch": "feat/other", "path": str(tmp_path / ".lab" / "second")},
            ]
            rows[1][duplicate_field] = rows[0][duplicate_field]
            return rows

        monkeypatch.setattr(WorktrunkAdapter, "_run", duplicate_cli)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            result = await success(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
            assert result["complete"] is False
            assert result["reason"] == "prune_preview_unavailable"
            assert _candidates(result) == []
            assert result["write_requested"] is False
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_cleanup_plan_unavailable_cli_fails_closed(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        original_which = shutil.which

        def without_worktrunk(command: str, *, mode: int = 1, path: str | None = None) -> str | None:
            if command == "wt":
                return None
            return original_which(command, mode=mode, path=path)

        monkeypatch.setattr(shutil, "which", without_worktrunk)
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            result = await success(client, "plan_project_worktree_cleanup", {"min_age_days": 0})
            assert result["complete"] is False
            assert result["reason"] == "worktrunk_unavailable"
            assert result["write_requested"] is False
            assert _candidates(result) == []
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())
