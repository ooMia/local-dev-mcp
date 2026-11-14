"""Verified linked-worktree picks preserve a dirty controller and commit only reviewed staging."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


async def _scenario(root: Path, workspace_factory: WorkspaceFactory) -> tuple[str, str, str, Path]:
    base, _ = await seed(root, workspace_factory)
    await git(root, "switch", "-c", "feat/source")
    (root / "feature.txt").write_text("first version\n")
    await git(root, "add", "feature.txt")
    await git(root, "commit", "-m", "feat: first")
    first = await git(root, "rev-parse", "HEAD")
    (root / "feature.txt").write_text("second version\n")
    await git(root, "commit", "-am", "feat: second")
    second = await git(root, "rev-parse", "HEAD")
    await git(root, "switch", "main")
    target = root / ".lab" / "linked-pick"
    await git(root, "worktree", "add", "-b", "feat/target", str(target), "main")
    return base, first, second, target


def _policy(root: Path, *, allow_wt: bool = True) -> Policy:
    allowed = ("git", "git.worktree.write") + (("wt",) if allow_wt else ())
    return replace(policy(root), allowed_commands=allowed)


def _request(base: str) -> dict[str, object]:
    return {
        "branch": "feat/target",
        "expected_branch_head": base,
        "expected_branch": "main",
        "expected_head": base,
    }


def test_worktree_pick_and_guarded_commits_preserve_dirty_controller(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, first, second, target = await _scenario(tmp_path, workspace_factory)
        (tmp_path / "base.txt").write_text("user's unsaved work\n")
        args = _request(base)
        async with Client(create_server(_policy(tmp_path))) as client:
            names = {tool.name: tool for tool in await client.list_tools()}
            for name in (
                "get_project_worktree_pick_state",
                "cherry_pick_project_worktree_commit",
                "commit_project_worktree_pick",
            ):
                assert name in names
                assert names[name].input_schema["additionalProperties"] is False
            assert "path" not in names["cherry_pick_project_worktree_commit"].input_schema["properties"]
            before = await success(client, "get_project_worktree_pick_state", args)
            assert before["branch"] == "feat/target"
            assert before["head"] == base
            assert before["worktree_path"] == ".lab/linked-pick"
            assert before["staged_paths"] == []
            assert before["unstaged_paths"] == []
            assert before["complete"] is True

            rejected = await failure(
                client,
                "cherry_pick_project_worktree_commit",
                {
                    **args,
                    "commit": first,
                    "expected_index_fingerprint": "f" * 64,
                },
            )
            assert "index changed" in rejected.lower()
            applied = await success(
                client,
                "cherry_pick_project_worktree_commit",
                {**args, "commit": first, "expected_index_fingerprint": before["index_fingerprint"]},
            )
            assert applied["outcome"] == "applied"
            assert applied["head_unchanged"] is True
            assert await git(target, "rev-parse", "HEAD") == base
            picked = await success(client, "get_project_worktree_pick_state", args)
            assert picked["staged_paths"] == ["feature.txt"]
            assert picked["unstaged_paths"] == []
            review = await success(
                client,
                "get_project_worktree_review",
                {"branch": "feat/target", "expected_head": base, "include_patch": True},
            )
            assert "first version" in str(cast(dict[str, object], review["patches"])["staged"])
            committed = await success(
                client,
                "commit_project_worktree_pick",
                {
                    **args,
                    "message": "feat(index): preserve reviewed first source",
                    "expected_index_fingerprint": picked["index_fingerprint"],
                    "expected_staged_paths": ["feature.txt"],
                },
            )
            new_head = str(committed["head_after"])
            assert committed["head_before"] == base
            assert committed["success"] is True
            assert await git(target, "rev-parse", "HEAD") == new_head
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            assert (tmp_path / "base.txt").read_text() == "user's unsaved work\n"

            next_args = {**args, "expected_branch_head": new_head}
            second_state = await success(client, "get_project_worktree_pick_state", next_args)
            applied2 = await success(
                client,
                "cherry_pick_project_worktree_commit",
                {
                    **next_args,
                    "commit": second,
                    "expected_index_fingerprint": second_state["index_fingerprint"],
                },
            )
            assert applied2["outcome"] == "applied"
            staged = await success(client, "get_project_worktree_pick_state", next_args)
            last = await success(
                client,
                "commit_project_worktree_pick",
                {
                    **next_args,
                    "message": "feat(index): preserve reviewed second source",
                    "expected_index_fingerprint": staged["index_fingerprint"],
                    "expected_staged_paths": ["feature.txt"],
                },
            )
            assert await git(target, "rev-parse", "HEAD") == last["head_after"]
            assert (target / "feature.txt").read_text() == "second version\n"
            assert (tmp_path / "base.txt").read_text() == "user's unsaved work\n"
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            final = await success(
                client,
                "get_project_worktree_pick_state",
                {**args, "expected_branch_head": last["head_after"]},
            )
            assert final["staged_paths"] == []
            assert final["unstaged_paths"] == []

    asyncio.run(scenario())


def test_pick_rejects_target_drift_and_user_file_collisions(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, first, _, target = await _scenario(tmp_path, workspace_factory)
        args = _request(base)
        async with Client(create_server(_policy(tmp_path))) as client:
            state = await success(client, "get_project_worktree_pick_state", args)
            wrong = await failure(
                client,
                "get_project_worktree_pick_state",
                {**args, "expected_branch_head": "f" * 40},
            )
            assert "unavailable" in wrong.lower() or "moved" in wrong.lower()
            (target / "feature.txt").write_text("user's untracked file\n")
            collision = await failure(
                client,
                "cherry_pick_project_worktree_commit",
                {
                    **args,
                    "commit": first,
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            assert "untracked or ignored" in collision.lower()
            assert (target / "feature.txt").read_text() == "user's untracked file\n"
            (target / "feature.txt").unlink()
            (target / "base.txt").write_text("target modified\n")
            dirty = await failure(
                client,
                "cherry_pick_project_worktree_commit",
                {
                    **args,
                    "commit": first,
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            assert "unstaged tracked" in dirty.lower()
            assert (target / "base.txt").read_text() == "target modified\n"
            assert await git(target, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


def test_worktree_commit_rejects_unreviewed_staging(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base, first, _, target = await _scenario(tmp_path, workspace_factory)
        args = _request(base)
        async with Client(create_server(_policy(tmp_path))) as client:
            before = await success(client, "get_project_worktree_pick_state", args)
            await success(
                client,
                "cherry_pick_project_worktree_commit",
                {**args, "commit": first, "expected_index_fingerprint": before["index_fingerprint"]},
            )
            staged = await success(client, "get_project_worktree_pick_state", args)
            rejected = await failure(
                client,
                "commit_project_worktree_pick",
                {
                    **args,
                    "message": "feat: incorrect staged scope",
                    "expected_index_fingerprint": staged["index_fingerprint"],
                    "expected_staged_paths": ["base.txt"],
                },
            )
            assert "staged paths differ" in rejected.lower()
            still = await success(client, "get_project_worktree_pick_state", args)
            assert still["staged_paths"] == ["feature.txt"]
            assert await git(target, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


def test_worktree_pick_requires_wt_permission_for_writes_not_read(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, first, _, target = await _scenario(tmp_path, workspace_factory)
        args = _request(base)
        async with Client(create_server(_policy(tmp_path, allow_wt=False))) as client:
            state = await success(client, "get_project_worktree_pick_state", args)
            assert state["head"] == base
            rejected = await failure(
                client,
                "cherry_pick_project_worktree_commit",
                {
                    **args,
                    "commit": first,
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            assert "not allowed" in rejected.lower() or "not available" in rejected.lower()
            assert await git(target, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


def test_worktree_pick_blocks_protected_source_files(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base, _, _, target = await _scenario(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "feat/source")
        (tmp_path / ".env.secret").write_text("PRIVATE=fixture\n")
        await git(tmp_path, "add", "-f", ".env.secret")
        await git(tmp_path, "commit", "-m", "test: protected fixture source")
        commit = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "main")
        args = _request(base)
        async with Client(create_server(_policy(tmp_path))) as client:
            before = await success(client, "get_project_worktree_pick_state", args)
            blocked = await failure(
                client,
                "cherry_pick_project_worktree_commit",
                {
                    **args,
                    "commit": commit,
                    "expected_index_fingerprint": before["index_fingerprint"],
                },
            )
            assert "protected path" in blocked.lower()
            after = await success(client, "get_project_worktree_pick_state", args)
            assert after["index_fingerprint"] == before["index_fingerprint"]
            assert await git(target, "rev-parse", "HEAD") == base
            assert not (target / ".env.secret").exists()

    asyncio.run(scenario())


def test_worktree_pick_conflict_is_reported_without_abort_or_reset(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, _, _, target = await _scenario(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "feat/source")
        (tmp_path / "base.txt").write_text("source edit\n")
        await git(tmp_path, "commit", "-am", "feat: conflicting source edit")
        commit = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "main")
        (target / "base.txt").write_text("target edit\n")
        await git(target, "commit", "-am", "feat: conflicting target edit")
        target_head = await git(target, "rev-parse", "HEAD")
        args = {**_request(base), "expected_branch_head": target_head}
        async with Client(create_server(_policy(tmp_path))) as client:
            before = await success(client, "get_project_worktree_pick_state", args)
            attempted = await success(
                client,
                "cherry_pick_project_worktree_commit",
                {
                    **args,
                    "commit": commit,
                    "expected_index_fingerprint": before["index_fingerprint"],
                },
            )
            assert attempted["outcome"] == "failed_or_conflicted"
            assert attempted["unmerged_paths"] == ["base.txt"]
            assert await git(target, "rev-parse", "HEAD") == target_head
            state = await success(client, "get_project_worktree_pick_state", args)
            assert "base.txt" in cast(list[str], state["staged_paths"])
            refused = await failure(
                client,
                "commit_project_worktree_pick",
                {
                    **args,
                    "message": "feat: should not commit unresolved conflict",
                    "expected_index_fingerprint": state["index_fingerprint"],
                    "expected_staged_paths": ["base.txt"],
                },
            )
            assert refused

    asyncio.run(scenario())
