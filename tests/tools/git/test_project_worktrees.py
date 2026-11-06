"""Real-Git isolated preparation and unchanged original checkout contracts."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, request, seed

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("sha256", [False, True])
def test_prepare_preserves_original_and_inspects_ready(
    tmp_path: Path, workspace_factory: WorkspaceFactory, sha256: bool
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory, sha256=sha256)
        original_index = await git(tmp_path, "ls-files", "--stage")
        async with Client(create_server(policy(tmp_path))) as session:
            absent = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert absent["state"] == "absent"
            ready = await success(session, "prepare_project_worktree", request(head))
            assert ready["state"] == "ready" and ready["head"] == head and ready["clean"] is True
            path = tmp_path / str(ready["path"])
            assert path.is_dir() and (path / ".git").is_file()
            assert not (path / ".env").exists()
            assert await git(path, "symbolic-ref", "--short", "HEAD") == "feat/publish"
            await failure(session, "prepare_project_worktree", request(head))
            inspected = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert inspected["state"] == "ready" and inspected["clean"] is True
            (path / "base.txt").write_text("dirty\n")
            dirty = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert dirty["clean"] is False
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        assert await git(tmp_path, "status", "--porcelain") == ""
        assert await git(tmp_path, "ls-files", "--stage") == original_index
        assert (tmp_path / "base.txt").read_text() == "base\n"

    asyncio.run(scenario())


def test_prepare_rejects_stale_ref_occupied_branch_and_missing_permission(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "prepare_project_worktree", {**request(head), "expected_branch_head": "a" * 40})
            await failure(session, "prepare_project_worktree", {**request(head), "branch": "main"})
            await failure(session, "prepare_project_worktree", {**request(head), "expected_head": "a" * 40})
            outside = tmp_path.parent / (tmp_path.name + "-occupied")
            await git(tmp_path, "worktree", "add", str(outside), "feat/publish")
            await failure(session, "prepare_project_worktree", request(head))
        denied = replace(policy(tmp_path), allowed_commands=("git",))
        async with Client(create_server(denied)) as session:
            await failure(session, "prepare_project_worktree", request(head))
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_prepare_refuses_unignored_or_symlink_managed_root(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        (tmp_path / ".gitignore").write_text(".env\n")
        await git(tmp_path, "add", ".gitignore")
        await git(tmp_path, "commit", "-m", "chore: remove ignore")
        current = await git(tmp_path, "rev-parse", "HEAD")
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "prepare_project_worktree", {**request(head), "expected_head": current})
        (tmp_path / ".gitignore").write_text(".env\n.lab/\n")
        await git(tmp_path, "add", ".gitignore")
        await git(tmp_path, "commit", "-m", "chore: restore ignore")
        current = await git(tmp_path, "rev-parse", "HEAD")
        outside = tmp_path.parent / (tmp_path.name + "-symlink")
        outside.mkdir()
        (tmp_path / ".lab").symlink_to(outside, target_is_directory=True)
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "prepare_project_worktree", {**request(head), "expected_head": current})
            await failure(session, "inspect_project_worktree", {"branch": "feat/publish"})
        assert list(outside.iterdir()) == []

    asyncio.run(scenario())


def test_prepare_respects_subtree_policy_and_rejects_foreign_worktree(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        denied = replace(policy(tmp_path), denied_paths=(tmp_path / ".lab",))
        async with Client(create_server(denied)) as session:
            await failure(session, "prepare_project_worktree", request(head))
        readonly = replace(policy(tmp_path), read_only_paths=(tmp_path / ".lab",))
        async with Client(create_server(readonly)) as session:
            await failure(session, "prepare_project_worktree", request(head))
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            path = tmp_path / str(ready["path"])
            await git(path, "switch", "--detach", head)
            report = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert report["state"] == "partial"
            await failure(session, "prepare_project_worktree", request(head))

    asyncio.run(scenario())


def test_new_tools_are_strict_and_permission_gated(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        async with Client(create_server(policy(tmp_path))) as session:
            declared = {entry.name: entry for entry in await session.list_tools()}
            names = {
                "prepare_project_worktree",
                "inspect_project_worktree",
                "inspect_project_branch_remote",
                "push_project_branch",
                "create_project_branch_draft_pr",
            }
            assert names <= declared.keys()
            for name in names:
                assert declared[name].input_schema["additionalProperties"] is False
            await failure(session, "prepare_project_worktree", {"branch": "--force"})
            for name in ("inspect_project_branch_remote", "push_project_branch", "create_project_branch_draft_pr"):
                await failure(session, name)

    asyncio.run(scenario())


def test_prepare_rejects_relative_denied_source_paths(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        restricted = replace(policy(tmp_path), denied_paths=(tmp_path / "base.txt",))
        async with Client(create_server(restricted)) as session:
            await failure(session, "prepare_project_worktree", request(head))
            state = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert state["state"] == "absent"

    asyncio.run(scenario())


def test_inspect_rejects_git_pointer_borrowed_from_another_worktree(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            outside = tmp_path.parent / (tmp_path.name + "-borrowed")
            await git(tmp_path, "worktree", "add", "--force", str(outside), "feat/publish")
            (target / ".git").write_text((outside / ".git").read_text())
            state = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert state["state"] == "partial"

    asyncio.run(scenario())


def test_prepare_preserves_ancestor_path_grants(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        parent_grant = replace(policy(tmp_path), allowed_paths=(tmp_path.parent,))
        async with Client(create_server(parent_grant)) as session:
            state = await success(session, "prepare_project_worktree", request(head))
            assert state["state"] == "ready" and state["clean"] is True

    asyncio.run(scenario())


def test_real_checkout_hook_failure_retains_partial_worktree(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        hook = tmp_path / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "prepare_project_worktree", request(head))
            state = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert state["state"] == "partial" and state["registered"] is True
            assert (tmp_path / str(state["path"]) / "base.txt").exists()
        assert await git(tmp_path, "status", "--porcelain") == ""
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_preexisting_managed_directory_is_never_adopted(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            state = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            path = tmp_path / str(state["path"])
            path.mkdir(parents=True)
            sentinel = path / "user.txt"
            sentinel.write_text("preserve")
            await failure(session, "prepare_project_worktree", request(head))
            state = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert state["state"] == "partial" and state["registered"] is False
            assert sentinel.read_text() == "preserve"

    asyncio.run(scenario())
