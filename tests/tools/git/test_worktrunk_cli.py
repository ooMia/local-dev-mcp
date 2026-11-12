"""Worktrunk adapter contract against isolated temporary Git repositories.

Never exercises Worktrunk against the developer's active checkout.
"""

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter
from tests.support.git import WorkspaceFactory, git
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


def _adapter(root: Path) -> WorktrunkAdapter:
    selected = policy(root)
    selected = replace(selected, allowed_commands=(*selected.allowed_commands, "wt"))
    return WorktrunkAdapter(ServerSettings.load(selected))


def _require_wt() -> None:
    if shutil.which("wt") is None:
        pytest.skip("Worktrunk CLI 'wt' is not installed on the active test host")


def test_worktrunk_create_preserves_checkout_and_refuses_collision(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        (tmp_path / "notes.txt").write_text("untracked user content\n")
        adapter = _adapter(tmp_path)
        created = await adapter.create("feat/wt-create", "main", head, "main", head)
        assert created["head"] == head
        path = tmp_path / created["path"]
        assert path.is_dir()
        assert await git(path, "symbolic-ref", "--short", "HEAD") == "feat/wt-create"
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        assert (tmp_path / "notes.txt").read_text() == "untracked user content\n"
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        with pytest.raises(ToolError):
            await adapter.create("feat/wt-create", "main", head, "main", head)
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [1, 3])
def test_worktrunk_squash_and_safe_removal(tmp_path: Path, workspace_factory: WorkspaceFactory, count: int) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        created = await adapter.create("feat/wt-merge", "main", head, "main", head)
        path = tmp_path / created["path"]
        for index in range(count):
            (path / f"change-{index}.txt").write_text(f"{index}\n")
            await git(path, "add", f"change-{index}.txt")
            await git(path, "commit", "-m", f"feat: add {index}")
        feature_head = await git(path, "rev-parse", "HEAD")
        result = await adapter.merge("feat/wt-merge", feature_head, head)
        assert result["main_after"] != head
        assert await git(tmp_path, "rev-parse", "HEAD") == result["main_after"]
        for index in range(count):
            assert (tmp_path / f"change-{index}.txt").read_text() == f"{index}\n"
        assert path.is_dir()  # --no-remove
        removed = await adapter.remove("feat/wt-merge", await git(path, "rev-parse", "HEAD"), delete_branch=True)
        assert removed["worktree_removed"] is True
        assert not path.exists()
        assert await git(tmp_path, "branch", "--list", "feat/wt-merge") == ""
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"

    asyncio.run(scenario())


def test_worktrunk_rejects_dirty_source_and_stale_main(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        created = await adapter.create("feat/wt-guard", "main", head, "main", head)
        path = tmp_path / created["path"]
        (path / "untracked.txt").write_text("preserve\n")
        with pytest.raises(ToolError, match="dirty"):
            await adapter.merge("feat/wt-guard", head, head)
        assert (path / "untracked.txt").read_text() == "preserve\n"
        with pytest.raises(ToolError, match="dirty"):
            await adapter.remove("feat/wt-guard", head, delete_branch=True)
        (path / "untracked.txt").unlink()
        (tmp_path / "main-change.txt").write_text("new main\n")
        await git(tmp_path, "add", "main-change.txt")
        await git(tmp_path, "commit", "-m", "fix: main advanced")
        with pytest.raises(ToolError, match="changed"):
            await adapter.merge("feat/wt-guard", head, head)
        assert path.exists()

    asyncio.run(scenario())


def test_worktrunk_permissions_and_list_schema(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        observed = await adapter.list_worktrees()
        assert observed["schema"] == 2
        assert isinstance(observed["items"], list)
        restricted = replace(policy(tmp_path), allowed_commands=("git",))
        with pytest.raises(ToolError):
            await WorktrunkAdapter(ServerSettings.load(restricted)).create("feat/denied", "main", head, "main", head)
        assert await git(tmp_path, "branch", "--list", "feat/denied") == ""
        denied_path = replace(
            policy(tmp_path), allowed_commands=("git", "git.worktree.write", "wt"), denied_paths=(tmp_path / ".lab",)
        )
        with pytest.raises(ToolError):
            await WorktrunkAdapter(ServerSettings.load(denied_path)).create("feat/denied", "main", head, "main", head)

    asyncio.run(scenario())


def test_worktrunk_rejects_foreign_worktree_and_diverged_main(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        await git(tmp_path, "branch", "feat/foreign", base)
        external = tmp_path.parent / (tmp_path.name + "-foreign")
        await git(tmp_path, "worktree", "add", str(external), "feat/foreign")
        with pytest.raises(ToolError, match="managed"):
            await adapter.remove("feat/foreign", base)
        assert external.exists()

        created = await adapter.create("feat/wt-diverged", "main", base, "main", base)
        source = tmp_path / created["path"]
        (source / "same.txt").write_text("source\n")
        await git(source, "add", "same.txt")
        await git(source, "commit", "-m", "feat: source")
        feature_head = await git(source, "rev-parse", "HEAD")
        (tmp_path / "same.txt").write_text("main\n")
        await git(tmp_path, "add", "same.txt")
        await git(tmp_path, "commit", "-m", "fix: main advanced")
        moved_main = await git(tmp_path, "rev-parse", "HEAD")
        with pytest.raises(ToolError, match="ancestor"):
            await adapter.merge("feat/wt-diverged", feature_head, moved_main)
        assert await git(tmp_path, "rev-parse", "HEAD") == moved_main
        assert await git(source, "rev-parse", "HEAD") == feature_head

    asyncio.run(scenario())


def test_worktrunk_rejects_protected_checkout_content(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    _require_wt()

    async def scenario() -> None:
        await seed(tmp_path, workspace_factory)
        await git(tmp_path, "add", "-f", ".env")
        await git(tmp_path, "commit", "-m", "test: tracked protected file")
        head = await git(tmp_path, "rev-parse", "HEAD")
        adapter = _adapter(tmp_path)
        with pytest.raises(ToolError, match="protected"):
            await adapter.create("feat/wt-protected", "main", head, "main", head)
        assert await git(tmp_path, "branch", "--list", "feat/wt-protected") == ""

    asyncio.run(scenario())
