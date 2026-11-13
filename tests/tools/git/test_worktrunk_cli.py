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
        reused = await adapter.create("feat/wt-create", "main", head, "main", head)
        assert reused["action"] == "reused"
        assert reused["path"] == created["path"]
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
        with pytest.raises(ToolError, match="dirty"):
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


def test_worktrunk_reuses_external_checkout_and_preserves_dirty_files(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        path = tmp_path / ".lab" / "external-checkout"
        await git(tmp_path, "worktree", "add", "-b", "feat/existing", str(path), "main")
        (path / "notes.txt").write_text("user changes\n")
        adapter = _adapter(tmp_path)
        reused = await adapter.create("feat/existing", "main", None, "main", head, expected_branch_head=head)
        assert reused == {
            "branch": "feat/existing",
            "head": head,
            "path": ".lab/external-checkout",
            "action": "reused",
        }
        assert (path / "notes.txt").read_text() == "user changes\n"
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        with pytest.raises(ToolError, match="HEAD changed"):
            await adapter.create("feat/existing", "main", None, "main", head, expected_branch_head="f" * 40)
        assert path.is_dir()

    asyncio.run(scenario())


def test_worktrunk_does_not_adopt_checkout_outside_workspace(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        external = tmp_path.parent / (tmp_path.name + "-outside-checkout")
        await git(tmp_path, "worktree", "add", "-b", "feat/outside-checkout", str(external), "main")
        adapter = _adapter(tmp_path)
        with pytest.raises(ToolError, match="authorized"):
            await adapter.create("feat/outside-checkout", "main", None, "main", head)
        assert external.exists()
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_worktrunk_checks_out_existing_branch_then_reuses_it(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        (tmp_path / "notes.txt").write_text("controller user data\n")
        adapter = _adapter(tmp_path)
        checked_out = await adapter.create("feat/publish", "main", None, "main", head, expected_branch_head=head)
        assert checked_out["action"] == "checked_out"
        path = tmp_path / checked_out["path"]
        assert path.is_dir()
        assert await git(path, "symbolic-ref", "--short", "HEAD") == "feat/publish"
        assert await git(path, "rev-parse", "HEAD") == head
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        assert (tmp_path / "notes.txt").read_text() == "controller user data\n"
        reused = await adapter.create("feat/publish", "main", None, "main", head)
        assert reused["action"] == "reused"
        assert reused["path"] == checked_out["path"]

    asyncio.run(scenario())


def test_worktrunk_existing_branch_cannot_overwrite_occupied_target(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        adapter = _adapter(tmp_path)
        import hashlib

        directory = tmp_path / ".lab" / "wt" / hashlib.sha256(b"feat/publish").hexdigest()[:24]
        directory.mkdir(parents=True)
        (directory / "user.txt").write_text("must keep\n")
        with pytest.raises(ToolError, match="occupied"):
            await adapter.create("feat/publish", "main", None, "main", head, expected_branch_head=head)
        assert (directory / "user.txt").read_text() == "must keep\n"
        assert await git(tmp_path, "worktree", "list", "--porcelain") != ""

    asyncio.run(scenario())


@pytest.mark.parametrize("branch_already_exists", [False, True])
def test_worktrunk_recovers_successful_switch_after_response_loss(
    tmp_path: Path,
    workspace_factory: WorkspaceFactory,
    monkeypatch: pytest.MonkeyPatch,
    branch_already_exists: bool,
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        branch = "feat/lost-response"
        if branch_already_exists:
            await git(tmp_path, "branch", branch, head)
        (tmp_path / "controller-notes.txt").write_text("preserve original changes\n")
        adapter = _adapter(tmp_path)
        original_run = WorktrunkAdapter._run  # pyright: ignore[reportPrivateUsage]  # Test-only fault injection.
        observed_path: list[Path] = []

        async def lose_response_after_success(self: WorktrunkAdapter, *argv: str, write: bool) -> None:
            result = await original_run(self, *argv, write=write)
            assert isinstance(result, dict)
            assert result["branch"] == branch
            observed_path.append(Path(str(result["path"])))
            raise ToolError("simulated response loss after successful Worktrunk switch")

        monkeypatch.setattr(WorktrunkAdapter, "_run", lose_response_after_success)
        with pytest.raises(ToolError, match="simulated response loss"):
            await adapter.create(
                branch,
                "main",
                None if branch_already_exists else head,
                "main",
                head,
                expected_branch_head=head if branch_already_exists else None,
            )
        monkeypatch.setattr(WorktrunkAdapter, "_run", original_run)

        assert len(observed_path) == 1
        target = observed_path[0]
        assert target.is_dir()
        (target / "recovery-notes.txt").write_text("new untracked work\n")
        recovered = await adapter.create(branch, "main", None, "main", head, expected_branch_head=head)
        assert recovered["action"] == "reused"
        assert recovered["path"] == str(target.relative_to(tmp_path))
        assert (target / "recovery-notes.txt").read_text() == "new untracked work\n"
        assert (tmp_path / "controller-notes.txt").read_text() == "preserve original changes\n"
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        assert await git(target, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


@pytest.mark.parametrize("existing", [True, False])
def test_worktrunk_ensure_does_not_limit_original_dirty_file_count(
    tmp_path: Path, workspace_factory: WorkspaceFactory, existing: bool
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        # The Native receipt snapshot refuses more than 256 dirty entries. Creating
        # a separate checkout does not need to read or hash their contents.
        for index in range(260):
            (tmp_path / f"user-{index}.txt").write_text(str(index))
        adapter = _adapter(tmp_path)
        if existing:
            target = tmp_path / ".lab" / "external-dirty-limit"
            await git(tmp_path, "worktree", "add", "-b", "feat/dirty-limit", str(target), "main")
        result = await adapter.create(
            "feat/dirty-limit",
            "main",
            None if existing else head,
            "main",
            head,
            expected_branch_head=head if existing else None,
        )
        assert result["action"] == ("reused" if existing else "created")
        assert (tmp_path / result["path"]).is_dir()
        assert all((tmp_path / f"user-{index}.txt").read_text() == str(index) for index in range(260))
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_worktrunk_rejects_a_registered_path_if_git_resolves_a_different_repository(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from local_dev_mcp.tools.git.commands.worktree import Worktree

        head, _ = await seed(tmp_path, workspace_factory)
        foreign = tmp_path / ".lab" / "foreign-repository"
        foreign.mkdir(parents=True)
        await git(foreign, "init")

        async def forged_registration(_execution: object) -> tuple[Worktree, ...]:
            return (Worktree(foreign, head, "refs/heads/feat/publish", False),)

        monkeypatch.setattr("local_dev_mcp.tools.git.worktrunk.adapter.worktree.list_registered", forged_registration)
        with pytest.raises(ToolError, match="not linked"):
            await _adapter(tmp_path).create("feat/publish", "main", None, "main", head, expected_branch_head=head)
        assert (foreign / ".git").exists()
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


@pytest.mark.parametrize("existing_branch", [False, True])
def test_worktrunk_explicit_workspace_path_creates_or_checks_out(
    tmp_path: Path, workspace_factory: WorkspaceFactory, existing_branch: bool
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        branch = "feat/custom-location"
        if existing_branch:
            await git(tmp_path, "branch", branch, head)
        # The directory is ignored, but is not the adapter's hashed managed path.
        custom = ".lab/custom/checkout"
        result = await _adapter(tmp_path).create(
            branch,
            "main",
            None if existing_branch else head,
            "main",
            head,
            expected_branch_head=head if existing_branch else None,
            worktree_path=custom,
        )
        assert result["path"] == custom
        assert result["action"] == ("checked_out" if existing_branch else "created")
        assert await git(tmp_path / custom, "rev-parse", "--show-toplevel") == str(tmp_path / custom)
        reused = await _adapter(tmp_path).create(
            branch, "main", None, "main", head, expected_branch_head=head, worktree_path=custom
        )
        assert reused["action"] == "reused"
        assert reused["path"] == custom

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad_path", ["../outside", "/tmp/outside", "C:/outside", ".git/checkout", "unignored/checkout"]
)
def test_worktrunk_explicit_path_rejects_unsafe_targets_without_creating_branch(
    tmp_path: Path, workspace_factory: WorkspaceFactory, bad_path: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        with pytest.raises(ToolError):
            await _adapter(tmp_path).create("feat/bad-path", "main", head, "main", head, worktree_path=bad_path)
        assert await git(tmp_path, "branch", "--list", "feat/bad-path") == ""

    asyncio.run(scenario())


def test_worktrunk_explicit_path_refuses_occupied_and_mismatched_locations(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        occupied = tmp_path / ".lab" / "custom" / "occupied"
        occupied.mkdir(parents=True)
        (occupied / "keep.txt").write_text("user content")
        with pytest.raises(ToolError, match="occupied"):
            await _adapter(tmp_path).create(
                "feat/occupied", "main", head, "main", head, worktree_path=".lab/custom/occupied"
            )
        assert (occupied / "keep.txt").read_text() == "user content"
        assert await git(tmp_path, "branch", "--list", "feat/occupied") == ""

        linked = tmp_path / ".lab" / "already-linked"
        await git(tmp_path, "worktree", "add", "-b", "feat/other-path", str(linked), "main")
        with pytest.raises(ToolError, match="path"):
            await _adapter(tmp_path).create(
                "feat/other-path",
                "main",
                None,
                "main",
                head,
                expected_branch_head=head,
                worktree_path=".lab/different-path",
            )
        assert linked.is_dir()

    asyncio.run(scenario())


def test_worktrunk_explicit_path_overrides_unsafe_user_layout(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    _require_wt()

    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        destination = tmp_path.parent / (tmp_path.name + "-outside-layout")
        original = _adapter(tmp_path)
        settings = replace(
            original.settings,
            environment=(*original.settings.environment, ("WORKTRUNK_WORKTREE_PATH", str(destination))),
        )
        adapter = WorktrunkAdapter(settings)
        result = await adapter.create(
            "feat/inside-layout", "main", head, "main", head, worktree_path=".lab/layout-inside"
        )
        assert result["path"] == ".lab/layout-inside"
        assert (tmp_path / ".lab" / "layout-inside").is_dir()
        assert not destination.exists()

    asyncio.run(scenario())


def test_worktrunk_explicit_path_rejects_symlink_parents(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        outside = tmp_path.parent / (tmp_path.name + "-symlink-target")
        outside.mkdir()
        parent = tmp_path / ".lab" / "linked-parent"
        parent.parent.mkdir(exist_ok=True)
        parent.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ToolError, match="symlink"):
            await _adapter(tmp_path).create(
                "feat/symlink-path",
                "main",
                head,
                "main",
                head,
                worktree_path=".lab/linked-parent/checkout",
            )
        assert not (outside / "checkout").exists()
        assert await git(tmp_path, "branch", "--list", "feat/symlink-path") == ""

    asyncio.run(scenario())


def test_inspect_worktrunk_distinguishes_absent_unassigned_and_dirty(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        adapter = WorktrunkAdapter(ServerSettings.load(policy(tmp_path)))
        absent = await adapter.inspect("feat/absent")
        assert (absent["state"], absent["registered"], absent["path"]) == ("absent", False, None)
        unassigned = await adapter.inspect("feat/publish")
        assert unassigned["state"] == "not_checked_out"
        assert unassigned["branch_head"] == base
        assert unassigned["path"] is None

        linked = tmp_path / ".lab" / "inspect-linked"
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        ready = await adapter.inspect("feat/publish")
        assert ready["state"] == "ready"
        assert ready["path"] == ".lab/inspect-linked"
        assert ready["head"] == ready["branch_head"] == base
        assert ready["clean"] is True

        (linked / "user-notes.txt").write_text("preserve dirty work")
        dirty = await adapter.inspect("feat/publish")
        assert dirty["state"] == "dirty"
        assert dirty["clean"] is False
        assert dirty["path"] == ".lab/inspect-linked"
        assert (linked / "user-notes.txt").read_text() == "preserve dirty work"
        assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


def test_inspect_worktrunk_blocks_outside_and_unavailable_registration(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from local_dev_mcp.tools.git.commands.worktree import Worktree

        head, _ = await seed(tmp_path, workspace_factory)
        external = tmp_path.parent / (tmp_path.name + "-inspect-outside")
        await git(tmp_path, "worktree", "add", str(external), "feat/publish")
        adapter = WorktrunkAdapter(ServerSettings.load(policy(tmp_path)))
        outside = await adapter.inspect("feat/publish")
        assert outside["state"] == "blocked"
        assert outside["reason"] == "outside_workspace"
        assert outside["path"] is None
        assert outside["registered"] is True
        assert external.is_dir()

        async def unavailable(_execution: object) -> tuple[Worktree, ...]:
            return (Worktree(external, head, "refs/heads/feat/publish", True),)

        monkeypatch.setattr("local_dev_mcp.tools.git.worktrunk.adapter.worktree.list_registered", unavailable)
        locked = await adapter.inspect("feat/publish")
        assert locked["state"] == "blocked"
        assert locked["reason"] == "registration_unavailable"
        assert locked["path"] is None

    asyncio.run(scenario())


def test_inspect_worktrunk_blocks_repository_and_head_mismatch(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from local_dev_mcp.tools.git.commands.worktree import Worktree

        head, _ = await seed(tmp_path, workspace_factory)
        linked = tmp_path / ".lab" / "inspect-branch"
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        adapter = WorktrunkAdapter(ServerSettings.load(policy(tmp_path)))
        foreign = tmp_path / ".lab" / "inspect-foreign"
        foreign.mkdir()
        await git(foreign, "init")

        async def unrelated(_execution: object) -> tuple[Worktree, ...]:
            return (Worktree(foreign, head, "refs/heads/feat/publish", False),)

        monkeypatch.setattr("local_dev_mcp.tools.git.worktrunk.adapter.worktree.list_registered", unrelated)
        invalid = await adapter.inspect("feat/publish")
        assert invalid["state"] == "blocked"
        assert invalid["reason"] == "repository_mismatch"
        assert invalid["path"] is None

        async def outdated(_execution: object) -> tuple[Worktree, ...]:
            return (Worktree(linked, "f" * 40, "refs/heads/feat/publish", False),)

        monkeypatch.setattr("local_dev_mcp.tools.git.worktrunk.adapter.worktree.list_registered", outdated)
        stale = await adapter.inspect("feat/publish")
        assert stale["state"] == "blocked"
        assert stale["reason"] == "head_mismatch"
        assert stale["path"] is None

    asyncio.run(scenario())


def test_worktrunk_readiness_keeps_write_access_gate(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        linked = tmp_path / ".lab" / "inspect-read-only"
        await git(tmp_path, "worktree", "add", str(linked), "feat/publish")
        existing = _adapter(tmp_path)
        restricted = replace(existing.settings.policy, read_only_paths=(linked,))
        adapter = WorktrunkAdapter(replace(existing.settings, policy=restricted))
        assert (await adapter.inspect("feat/publish"))["state"] == "ready"
        with pytest.raises(ToolError, match="not allowed"):
            await adapter.require_ready("feat/publish", head)
        assert await git(linked, "rev-parse", "HEAD") == head

    asyncio.run(scenario())
