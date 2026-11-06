"""Selective Git migration commands operate on fixture repositories, never the project repo."""

import asyncio
from pathlib import Path
from typing import cast

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration


async def _initial(root: Path) -> None:
    (root / "shared.txt").write_text("base\n")
    await git(root, "add", "shared.txt")
    await git(root, "commit", "-m", "base")


async def _feature(root: Path) -> tuple[str, str]:
    await git(root, "switch", "-c", "feature")
    (root / "core.txt").write_text("v1\n")
    (root / "irrelevant.txt").write_text("unrelated\n")
    await git(root, "add", "core.txt", "irrelevant.txt")
    await git(root, "commit", "-m", "one")
    first = await git(root, "rev-parse", "HEAD")
    (root / "core.txt").write_text("v2\n")
    await git(root, "commit", "-am", "two")
    second = await git(root, "rev-parse", "HEAD")
    await git(root, "switch", "-")
    return first, second


def test_compare_refs_pins_both_revisions(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _initial(tmp_path)
        base = await git(tmp_path, "rev-parse", "HEAD")
        _, end = await _feature(tmp_path)
        async with client(tmp_path) as session:
            diff = await success(
                session,
                "compare_project_refs",
                {
                    "base_ref": base,
                    "target_ref": "feature",
                    "expected_base_head": base,
                    "expected_target_head": end,
                },
            )
            assert diff["complete"] is True
            assert [record["path"] for record in cast(list[dict[str, object]], diff["changes"])] == [
                "core.txt",
                "irrelevant.txt",
            ]
            assert "v2" in str(diff["patch"])
            assert await failure(
                session,
                "compare_project_refs",
                {
                    "base_ref": base,
                    "target_ref": "feature",
                    "expected_base_head": base,
                    "expected_target_head": "f" * 40,
                },
            )
            assert await failure(
                session,
                "compare_project_refs",
                {
                    "base_ref": "HEAD~1",
                    "target_ref": "feature",
                    "expected_base_head": base,
                    "expected_target_head": end,
                },
            )

    asyncio.run(scenario())


def test_pick_restore_and_continue_preserve_untracked_and_do_not_commit(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _initial(tmp_path)
        first, second = await _feature(tmp_path)
        (tmp_path / "handoff.md").write_text("user data\n")
        async with client(tmp_path) as session:
            state = await success(session, "get_project_pick_state")
            args = {
                "expected_branch": state["branch"],
                "expected_head": state["head"],
                "expected_index_fingerprint": state["index_fingerprint"],
            }
            applied = await success(session, "cherry_pick_project_commit", {"commit": first, **args})
            assert applied["outcome"] == "applied", applied
            assert applied["head_unchanged"] is True
            assert await git(tmp_path, "rev-parse", "HEAD") == state["head"]
            staged = await success(session, "get_project_diff", {"staged": True})
            assert "irrelevant.txt" in str(staged["patch"])
            after_first = await success(session, "get_project_pick_state")
            assert after_first["staged_paths"] == ["core.txt", "irrelevant.txt"]
            restored = await success(
                session,
                "restore_project_pick_paths",
                {
                    "paths": ["irrelevant.txt"],
                    "expected_branch": state["branch"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": after_first["index_fingerprint"],
                },
            )
            assert restored["head_unchanged"] is True
            assert not (tmp_path / "irrelevant.txt").exists()
            after_restore = await success(session, "get_project_pick_state")
            assert after_restore["staged_paths"] == ["core.txt"]
            applied2 = await success(
                session,
                "cherry_pick_project_commit",
                {
                    "commit": second,
                    "expected_branch": state["branch"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": after_restore["index_fingerprint"],
                    "allow_staged": True,
                },
            )
            assert applied2["outcome"] == "applied", applied2
            final = await success(session, "get_project_pick_state")
            assert final["head"] == state["head"]
            assert final["staged_paths"] == ["core.txt"]
            assert final["unstaged_paths"] == []
            assert final["untracked_paths"] == ["handoff.md"]
        assert (tmp_path / "core.txt").read_text() == "v2\n"
        assert (tmp_path / "handoff.md").read_text() == "user data\n"

    asyncio.run(scenario())


def test_pick_rejects_stale_index_and_unstaged_tracked_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _initial(tmp_path)
        first, _ = await _feature(tmp_path)
        async with client(tmp_path) as session:
            state = await success(session, "get_project_pick_state")
            base = {
                "commit": first,
                "expected_branch": state["branch"],
                "expected_head": state["head"],
            }
            error = await failure(
                session,
                "cherry_pick_project_commit",
                {
                    **base,
                    "expected_index_fingerprint": "f" * 64,
                },
            )
            assert "index changed" in error.lower()
            (tmp_path / "shared.txt").write_text("user change\n")
            error = await failure(
                session,
                "cherry_pick_project_commit",
                {
                    **base,
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            assert "unstaged tracked" in error.lower()
            after = await success(session, "get_project_pick_state")
            assert after["head"] == state["head"]
            assert after["staged_paths"] == []
        assert (tmp_path / "shared.txt").read_text() == "user change\n"

    asyncio.run(scenario())


def test_pick_reports_conflict_without_rollback(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _initial(tmp_path)
        await git(tmp_path, "switch", "-c", "feature")
        (tmp_path / "shared.txt").write_text("feature\n")
        await git(tmp_path, "commit", "-am", "feature change")
        feature_commit = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "-")
        (tmp_path / "shared.txt").write_text("current\n")
        await git(tmp_path, "commit", "-am", "current change")
        async with client(tmp_path) as session:
            state = await success(session, "get_project_pick_state")
            result = await success(
                session,
                "cherry_pick_project_commit",
                {
                    "commit": feature_commit,
                    "expected_branch": state["branch"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            assert result["outcome"] == "failed_or_conflicted"
            assert result["unmerged_paths"] == ["shared.txt"]
            after = await success(session, "get_project_pick_state")
            assert after["head"] == state["head"]
            assert after["staged_paths"] == ["shared.txt"]
            assert await failure(
                session,
                "cherry_pick_project_commit",
                {
                    "commit": feature_commit,
                    "expected_branch": state["branch"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": after["index_fingerprint"],
                    "allow_staged": True,
                },
            )

    asyncio.run(scenario())


def test_restore_refuses_stale_index_or_unstaged_changes(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _initial(tmp_path)
        first, _ = await _feature(tmp_path)
        async with client(tmp_path) as session:
            start = await success(session, "get_project_pick_state")
            await success(
                session,
                "cherry_pick_project_commit",
                {
                    "commit": first,
                    "expected_branch": start["branch"],
                    "expected_head": start["head"],
                    "expected_index_fingerprint": start["index_fingerprint"],
                },
            )
            after = await success(session, "get_project_pick_state")
            expected = {
                "paths": ["irrelevant.txt"],
                "expected_branch": start["branch"],
                "expected_head": start["head"],
            }
            assert await failure(
                session,
                "restore_project_pick_paths",
                {
                    **expected,
                    "expected_index_fingerprint": "f" * 64,
                },
            )
            assert await failure(
                session,
                "restore_project_pick_paths",
                {
                    **expected,
                    "paths": ["."],
                    "expected_index_fingerprint": after["index_fingerprint"],
                },
            )
            (tmp_path / "core.txt").write_text("manual extra edit\n")
            assert await failure(
                session,
                "restore_project_pick_paths",
                {
                    **expected,
                    "expected_index_fingerprint": after["index_fingerprint"],
                },
            )
        assert (tmp_path / "irrelevant.txt").read_text() == "unrelated\n"

    asyncio.run(scenario())


def test_pick_rejects_commits_touching_protected_paths(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _initial(tmp_path)
        await git(tmp_path, "switch", "-c", "sensitive")
        (tmp_path / ".env.secret").write_text("PRIVATE=fixture\n")
        await git(tmp_path, "add", "-f", ".env.secret")
        await git(tmp_path, "commit", "-m", "sensitive source")
        source = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "-")
        async with client(tmp_path) as session:
            state = await success(session, "get_project_pick_state")
            error = await failure(
                session,
                "cherry_pick_project_commit",
                {
                    "commit": source,
                    "expected_branch": state["branch"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            assert "protected path" in error.lower()
            after = await success(session, "get_project_pick_state")
            assert after["head"] == state["head"]
            assert after["staged_paths"] == []

    asyncio.run(scenario())


def test_pick_preserves_existing_ignored_file(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _initial(tmp_path)
        (tmp_path / ".gitignore").write_text("*.cache\n")
        await git(tmp_path, "add", ".gitignore")
        await git(tmp_path, "commit", "-m", "ignore cache")
        await git(tmp_path, "switch", "-c", "feature-cache")
        (tmp_path / "user.cache").write_text("feature content\n")
        await git(tmp_path, "add", "-f", "user.cache")
        await git(tmp_path, "commit", "-m", "include ignored file")
        source = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "-")
        (tmp_path / "user.cache").write_text("user private content\n")
        async with client(tmp_path) as session:
            state = await success(session, "get_project_pick_state")
            result = await session.call_tool(
                "cherry_pick_project_commit",
                {
                    "commit": source,
                    "expected_branch": state["branch"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
                raise_on_error=False,
            )
            assert result.is_error or result.structured_content is not None
            after = await success(session, "get_project_pick_state")
            assert after["head"] == state["head"]
        assert (tmp_path / "user.cache").read_text() == "user private content\n"

    asyncio.run(scenario())
