"""Ref-based branch creation and guarded switching through the MCP boundary."""

import asyncio
from pathlib import Path

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration


async def _seed(root: Path) -> None:
    (root / "tracked.txt").write_text("initial\n")
    await git(root, "add", "tracked.txt")
    await git(root, "commit", "-m", "seed")


def test_get_project_ref_limits_inputs_to_local_branches_and_full_commits(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "branch", "base-stable")
        await git(tmp_path, "tag", "release-test")
        async with client(tmp_path) as session:
            current = await success(session, "get_project_status")
            head = current["head"]
            assert isinstance(head, str)
            branch = await success(session, "get_project_ref", {"ref": "base-stable"})
            commit = await success(session, "get_project_ref", {"ref": head})
            assert branch["ref"] == "base-stable"
            assert commit["ref"] == head
            assert branch["head"] == commit["head"] == head
            for invalid in ("refs/heads/base-stable", "origin/main", "release-test", "HEAD~1", head[:12]):
                error = await failure(session, "get_project_ref", {"ref": invalid})
                assert error

    asyncio.run(scenario())


def test_create_from_ref_does_not_switch_or_modify_index_or_untracked(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "branch", "base-stable")
        (tmp_path / "tracked.txt").write_text("staged edit\n")
        await git(tmp_path, "add", "tracked.txt")
        (tmp_path / "handoff.md").write_text("preserve\n")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            base = await success(session, "get_project_ref", {"ref": "base-stable"})
            result = await success(
                session,
                "create_project_branch_from_ref",
                {
                    "new_branch": "migration/document-core",
                    "base_ref": "base-stable",
                    "expected_base_head": base["head"],
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                },
            )
            after = await success(session, "get_project_status")
            target = await success(session, "get_project_ref", {"ref": "migration/document-core"})
        assert result["head"] == base["head"]
        assert result["branch_unchanged"] is True
        assert target["head"] == base["head"]
        assert (before["branch"], before["head"], before["entries"]) == (
            after["branch"],
            after["head"],
            after["entries"],
        )
        assert (tmp_path / "tracked.txt").read_text() == "staged edit\n"
        assert (tmp_path / "handoff.md").read_text() == "preserve\n"

    asyncio.run(scenario())


def test_create_from_ref_rejects_stale_base_and_existing_branch(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "branch", "base-stable")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            arguments = {
                "new_branch": "migration/document-core",
                "base_ref": "base-stable",
                "expected_base_head": "f" * 40,
                "expected_branch": before["branch"],
                "expected_head": before["head"],
            }
            error = await failure(session, "create_project_branch_from_ref", arguments)
            assert "base ref moved" in error.lower()
            assert await git(tmp_path, "branch", "--list", "migration/document-core") == ""
            arguments["expected_base_head"] = before["head"]
            await success(session, "create_project_branch_from_ref", arguments)
            error = await failure(session, "create_project_branch_from_ref", arguments)
            assert "already exists" in error.lower()

    asyncio.run(scenario())


def test_switch_preserves_noncolliding_untracked_files(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "branch", "base-stable")
        (tmp_path / "tracked.txt").write_text("feature change\n")
        await git(tmp_path, "commit", "-am", "feature")
        (tmp_path / "handoff.md").write_text("important user data\n")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            target = await success(session, "get_project_ref", {"ref": "base-stable"})
            result = await success(
                session,
                "switch_project_branch",
                {
                    "target_branch": "base-stable",
                    "expected_target_head": target["head"],
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                },
            )
            after = await success(session, "get_project_status")
        assert result["untracked_paths_unchanged"] is True
        assert result["head_after"] == target["head"]
        assert after["branch"] == "base-stable"
        assert (tmp_path / "handoff.md").read_text() == "important user data\n"
        assert (tmp_path / "tracked.txt").read_text() == "initial\n"

    asyncio.run(scenario())


@pytest.mark.parametrize("staged", [False, True])
def test_switch_rejects_dirty_tracked_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, staged: bool
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "branch", "base-stable")
        (tmp_path / "tracked.txt").write_text("do not lose\n")
        if staged:
            await git(tmp_path, "add", "tracked.txt")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            error = await failure(
                session,
                "switch_project_branch",
                {
                    "target_branch": "base-stable",
                    "expected_target_head": before["head"],
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                },
            )
            after = await success(session, "get_project_status")
            assert "clean" in error.lower()
            assert after["branch"] == before["branch"]
            assert after["entries"] == before["entries"]
        assert (tmp_path / "tracked.txt").read_text() == "do not lose\n"

    asyncio.run(scenario())


def test_switch_refuses_untracked_file_collision(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "switch", "-c", "target-docs")
        (tmp_path / "handoff.md").write_text("tracked target\n")
        await git(tmp_path, "add", "handoff.md")
        await git(tmp_path, "commit", "-m", "add target file")
        await git(tmp_path, "switch", "-")
        (tmp_path / "handoff.md").write_text("untracked user data\n")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            target = await success(session, "get_project_ref", {"ref": "target-docs"})
            error = await failure(
                session,
                "switch_project_branch",
                {
                    "target_branch": "target-docs",
                    "expected_target_head": target["head"],
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                },
            )
            after = await success(session, "get_project_status")
        assert "switch" in error.lower() or "git exit code" in error.lower()
        assert after["branch"] == before["branch"]
        assert (tmp_path / "handoff.md").read_text() == "untracked user data\n"

    asyncio.run(scenario())


def test_switch_rejects_stale_target_sha(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "branch", "base-stable")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            error = await failure(
                session,
                "switch_project_branch",
                {
                    "target_branch": "base-stable",
                    "expected_target_head": "f" * 40,
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                },
            )
            after = await success(session, "get_project_status")
        assert "target branch changed" in error.lower()
        assert after["branch"] == before["branch"]

    asyncio.run(scenario())


def test_get_project_ref_supports_full_sha256_commit_ids(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory(sha256=True)
        await _seed(tmp_path)
        async with client(tmp_path) as session:
            status = await success(session, "get_project_status")
            sha = status["head"]
            assert isinstance(sha, str) and len(sha) == 64
            resolved = await success(session, "get_project_ref", {"ref": sha})
            assert resolved["ref"] == sha
            assert resolved["head"] == sha

    asyncio.run(scenario())


def test_switch_refuses_ignored_file_collision(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        (tmp_path / ".gitignore").write_text("*.cache\n")
        await git(tmp_path, "add", ".gitignore")
        await git(tmp_path, "commit", "-m", "ignore cache")
        await git(tmp_path, "switch", "-c", "target-cache")
        (tmp_path / "keep.cache").write_text("tracked target\n")
        await git(tmp_path, "add", "-f", "keep.cache")
        await git(tmp_path, "commit", "-m", "track cache on target")
        await git(tmp_path, "switch", "-")
        (tmp_path / "keep.cache").write_text("ignored user data\n")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            target = await success(session, "get_project_ref", {"ref": "target-cache"})
            error = await failure(
                session,
                "switch_project_branch",
                {
                    "target_branch": "target-cache",
                    "expected_target_head": target["head"],
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                },
            )
            after = await success(session, "get_project_status")
        assert error
        assert after["branch"] == before["branch"]
        assert (tmp_path / "keep.cache").read_text() == "ignored user data\n"

    asyncio.run(scenario())
