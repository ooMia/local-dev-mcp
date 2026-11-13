"""Read-only inventory preserves path boundaries and validates optional Worktrunk facts."""

import asyncio
import os
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


def _items(result: dict[str, object]) -> list[dict[str, object]]:
    value = result["items"]
    assert isinstance(value, list)
    return cast(list[dict[str, object]], value)


def test_inventory_git_only_lists_registered_and_branch_only_rows(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        linked = tmp_path / ".lab" / "feature"
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        await git(tmp_path, "branch", "feat/unopened", head)
        permissions = replace(policy(tmp_path), allowed_commands=("git",))
        async with Client(create_server(permissions)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert tools["list_project_worktrees"].annotations is not None
            assert tools["list_project_worktrees"].annotations.read_only_hint
            result = await success(client, "list_project_worktrees", {})
            assert result["complete"] is True
            assert result["enrichment"] == "not_available"
            rows = {row["branch"]: row for row in _items(result)}
            assert rows["main"]["path"] == "." and rows["main"]["head"] == head
            assert rows["feat/publish"]["path"] == ".lab/feature"
            assert rows["feat/publish"]["state"] == "ready"
            assert rows["feat/unopened"]["state"] == "not_checked_out"
            assert rows["feat/unopened"]["path"] is None
            assert rows["feat/publish"]["ahead"] is None
            only = await success(client, "list_project_worktrees", {"include_branches": False})
            assert "feat/unopened" not in {row["branch"] for row in _items(only)}
            await failure(client, "list_project_worktrees", {"include_remote": True})
            await failure(client, "create_project_worktrunk", {"branch": "feat/new"})
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_inventory_omits_outside_and_denied_worktrees_without_branch_disclosure(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        denied = tmp_path / ".lab" / "denied"
        await git(tmp_path, "worktree", "add", "-b", "feat/outside", str(outside), "main")
        await git(tmp_path, "worktree", "add", "-b", "feat/denied", str(denied), "main")
        permissions = replace(policy(tmp_path), allowed_commands=("git",), denied_paths=(denied,))
        async with Client(create_server(permissions)) as client:
            result = await success(client, "list_project_worktrees", {})
            assert result["blocked_count"] == 2
            names = {row["branch"] for row in _items(result)}
            assert names == {"main", "feat/publish"}
            assert str(outside) not in str(result)
            assert str(denied) not in str(result)
            assert "feat/outside" not in str(result)
            assert "feat/denied" not in str(result)
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_inventory_enriches_only_when_worktrunk_matches_git(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        linked = tmp_path / ".lab" / "feature"
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        permissions = replace(policy(tmp_path), allowed_commands=("git", "wt"))

        async def wt_list(self: WorktrunkAdapter, *, include_branches: bool = False) -> dict[str, object]:
            assert include_branches
            return {
                "schema": 2,
                "items": [
                    {
                        "branch": "main",
                        "head": {"sha": head},
                        "worktree": {"path": str(tmp_path), "changes": {"conflicted": False}},
                        "default_branch": {"ahead": 0, "behind": 0, "merge_conflicts": False},
                    },
                    {
                        "branch": "feat/publish",
                        "head": {"sha": head},
                        "worktree": {"path": str(linked), "changes": {"conflicted": False}},
                        "default_branch": {"ahead": 2, "behind": 1, "merge_conflicts": False},
                    },
                ],
            }

        original_which = shutil.which

        def fake_which(name: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
            return "wt" if name == "wt" else original_which(name, mode=mode, path=path)

        monkeypatch.setattr("local_dev_mcp.tools.git.worktrunk.inventory.shutil.which", fake_which)
        monkeypatch.setattr(WorktrunkAdapter, "list_worktrees", wt_list)
        async with Client(create_server(permissions)) as client:
            result = await success(client, "list_project_worktrees", {})
            assert result["enrichment"] == "verified" and result["complete"] is True
            rows = {row["branch"]: row for row in _items(result)}
            assert rows["feat/publish"]["ahead"] == 2
            assert rows["feat/publish"]["behind"] == 1

            async def mismatched(self: WorktrunkAdapter, *, include_branches: bool = False) -> dict[str, object]:
                data = await wt_list(self, include_branches=include_branches)
                rows = _items(data)
                head_object = cast(dict[str, object], rows[1]["head"])
                head_object["sha"] = "0" * 40
                return data

            monkeypatch.setattr(WorktrunkAdapter, "list_worktrees", mismatched)
            bad = await success(client, "list_project_worktrees", {})
            assert bad["complete"] is False
            assert bad["enrichment"] == "schema_or_identity_mismatch"
            bad_rows = {row["branch"]: row for row in _items(bad)}
            assert bad_rows["feat/publish"]["ahead"] is None
            assert bad_rows["main"]["ahead"] is None

    asyncio.run(scenario())


def test_inventory_matches_real_worktrunk_json_schema(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    if shutil.which("wt") is None:
        pytest.skip("Worktrunk CLI is unavailable on this host")

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        path = tmp_path / ".lab" / "wt-real"
        await git(tmp_path, "worktree", "add", str(path), "feat/publish")
        async with Client(create_server(replace(policy(tmp_path), allowed_commands=("git", "wt")))) as client:
            result = await success(client, "list_project_worktrees", {})
            assert result["complete"] is True, result
            assert result["enrichment"] == "verified"
            named = {row["branch"]: row for row in _items(result)}
            assert named["feat/publish"]["head"] == head
            assert named["feat/publish"]["path"] == ".lab/wt-real"
            assert named["feat/publish"]["ahead"] is not None
            assert named["main"]["head"] == head

    asyncio.run(scenario())
