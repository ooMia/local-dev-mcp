"""Worktrunk create/remove adapter and external merge CLI in temporary Git repositories.

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


async def _external_worktrunk_merge(root: Path, branch: str) -> None:
    """Run external CLI in a disposable repository, never through the MCP adapter."""
    binary = shutil.which("wt")
    assert binary is not None
    process = await asyncio.create_subprocess_exec(
        binary,
        "-C",
        str(root),
        "merge",
        "--branch",
        branch,
        "--no-rebase",
        "--no-remove",
        "--stage",
        "none",
        "--no-hooks",
        "main",
        "--format",
        "json",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode == 0, (stdout.decode(errors="replace"), stderr.decode(errors="replace"))


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
        assert await git(path, "rev-parse", "HEAD") != head
        await _external_worktrunk_merge(tmp_path, "feat/wt-merge")
        main_after = await git(tmp_path, "rev-parse", "HEAD")
        assert main_after != head
        assert await git(tmp_path, "rev-parse", "main") == main_after
        for index in range(count):
            assert (tmp_path / f"change-{index}.txt").read_text() == f"{index}\n"
        assert path.is_dir()  # --no-remove
        removed = await adapter.remove("feat/wt-merge", await git(path, "rev-parse", "HEAD"), delete_branch=True)
        assert removed["worktree_removed"] is True
        assert not path.exists()
        assert await git(tmp_path, "branch", "--list", "feat/wt-merge") == ""
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"

    asyncio.run(scenario())


def test_worktrunk_rejects_dirty_source_and_preserves_untracked_files(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        created = await adapter.create("feat/wt-guard", "main", head, "main", head)
        path = tmp_path / created["path"]
        (path / "untracked.txt").write_text("preserve\n")
        with pytest.raises(ToolError, match="dirty"):
            await adapter.require_ready("feat/wt-guard", head)
        assert (path / "untracked.txt").read_text() == "preserve\n"
        with pytest.raises(ToolError, match="Worktrunk failed"):
            await adapter.remove("feat/wt-guard", head, delete_branch=True)
        (path / "untracked.txt").unlink()
        assert (await adapter.require_ready("feat/wt-guard", head)).policy.root == path
        assert path.exists()
        assert await git(tmp_path, "rev-parse", "HEAD") == head

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


def test_worktrunk_rejects_foreign_worktree(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    _require_wt()

    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        await git(tmp_path, "branch", "feat/foreign", base)
        external = tmp_path.parent / (tmp_path.name + "-foreign")
        await git(tmp_path, "worktree", "add", str(external), "feat/foreign")
        with pytest.raises(ToolError, match="not allowed"):
            await adapter.remove("feat/foreign", base)
        assert external.exists()

        assert await git(tmp_path, "rev-parse", "HEAD") == base

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


def test_worktrunk_rejects_denied_source_before_creating_branch(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        restricted = replace(
            policy(tmp_path),
            allowed_commands=("git", "git.worktree.write", "wt"),
            denied_paths=(tmp_path / "base.txt",),
        )
        adapter = WorktrunkAdapter(ServerSettings.load(restricted))
        with pytest.raises(ToolError, match="access policy"):
            await adapter.create("feat/wt-source-denied", "main", head, "main", head)
        assert await git(tmp_path, "branch", "--list", "feat/wt-source-denied") == ""
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_worktrunk_removes_external_checkout_inside_allowed_workspace(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        path = tmp_path / ".lab" / "user-worktree"
        await git(tmp_path, "worktree", "add", "-b", "feat/user-worktree", str(path), "main")
        adapter = _adapter(tmp_path)
        removed = await adapter.remove("feat/user-worktree", head, delete_branch=False)
        assert removed["worktree_removed"] is True
        assert removed["branch_deleted"] is False
        assert removed["branch_outcome"] == "not_attempted"
        assert not path.exists()
        assert await git(tmp_path, "branch", "--list", "feat/user-worktree") != ""

    asyncio.run(scenario())


def test_worktrunk_default_removal_delegates_branch_cleanup(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        created = await adapter.create("feat/wt-default-cleanup", "main", head, "main", head)
        removed = await adapter.remove("feat/wt-default-cleanup", head)
        assert removed["worktree_removed"] is True
        assert removed["branch_outcome"] == "deleted"
        assert removed["branch_deleted"] is True
        assert not (tmp_path / created["path"]).exists()
        assert await git(tmp_path, "branch", "--list", "feat/wt-default-cleanup") == ""

    asyncio.run(scenario())


def test_worktrunk_retain_unmerged_branch_even_if_deletion_requested(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        created = await adapter.create("feat/wt-unmerged", "main", head, "main", head)
        path = tmp_path / created["path"]
        (path / "unmerged.txt").write_text("new work\n")
        await git(path, "add", "unmerged.txt")
        await git(path, "commit", "-m", "feat: work is not integrated")
        changed_head = await git(path, "rev-parse", "HEAD")
        removed = await adapter.remove("feat/wt-unmerged", changed_head, delete_branch=True)
        assert removed["branch_outcome"] == "retained_unmerged"
        assert removed["branch_deleted"] is False
        assert not path.exists()
        assert await git(tmp_path, "branch", "--list", "feat/wt-unmerged") != ""

    asyncio.run(scenario())


@pytest.mark.parametrize("run_hooks", [False, True])
def test_worktrunk_hooks_are_opt_in_for_create_and_remove(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, run_hooks: bool
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        calls: list[tuple[str, ...]] = []

        async def record_without_running(self: WorktrunkAdapter, *argv: str, write: bool) -> dict[str, object]:
            assert write is True
            calls.append(argv)
            raise ToolError("intercepted before Worktrunk execution")

        monkeypatch.setattr(WorktrunkAdapter, "_run", record_without_running)
        with pytest.raises(ToolError, match="intercepted"):
            await adapter.create("feat/hook-create", "main", head, "main", head, run_hooks=run_hooks)

        path = tmp_path / ".lab" / "hook-remove"
        await git(tmp_path, "worktree", "add", "-b", "feat/hook-remove", str(path), "main")
        with pytest.raises(ToolError, match="intercepted"):
            await adapter.remove("feat/hook-remove", head, run_hooks=run_hooks, delete_branch=False)

        assert len(calls) == 2
        assert calls[0][0] == "switch"
        assert calls[1][0] == "remove"
        assert all(("--yes" in args) is run_hooks for args in calls)
        assert all(("--no-hooks" in args) is (not run_hooks) for args in calls)
        assert path.exists()
        assert await git(tmp_path, "branch", "--list", "feat/hook-create") == ""

    asyncio.run(scenario())
