"""Contract tests for explicitly authorized local branch isolation."""

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


def test_create_branch_preserves_uncommitted_tracked_and_untracked_files(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        (tmp_path / "tracked.txt").write_text("edited\n")
        (tmp_path / "untracked.txt").write_text("new\n")

        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            assert before["head"] is not None
            result = await success(
                session,
                "create_project_branch",
                {
                    "new_branch": "wip/github-automation-20261030",
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                },
            )
            after = await success(session, "get_project_status")

        assert result["success"] is True
        assert result["branch_before"] == before["branch"]
        assert result["branch_after"] == "wip/github-automation-20261030"
        assert result["head_before"] == result["head_after"] == before["head"]
        assert result["status_unchanged"] is True
        assert after["branch"] == result["branch_after"]
        assert after["head"] == before["head"]
        assert after["entries"] == before["entries"]
        assert (tmp_path / "tracked.txt").read_text() == "edited\n"
        assert (tmp_path / "untracked.txt").read_text() == "new\n"
        assert isinstance(before["branch"], str)
        assert await git(tmp_path, "rev-parse", "--verify", before["branch"]) == before["head"]

    asyncio.run(scenario())


def test_branch_creation_requires_matching_observed_baseline(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            error = await failure(
                session,
                "create_project_branch",
                {"new_branch": "wip/test", "expected_branch": before["branch"], "expected_head": "f" * 40},
            )
            assert "baseline changed" in error.lower()
            after = await success(session, "get_project_status")
            assert after == {**before, "observed_at": after["observed_at"]}
        assert await git(tmp_path, "branch", "--list", "wip/test") == ""

    asyncio.run(scenario())


def test_branch_creation_refuses_staged_changes(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        (tmp_path / "tracked.txt").write_text("staged\n")
        await git(tmp_path, "add", "tracked.txt")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            error = await failure(
                session,
                "create_project_branch",
                {"new_branch": "wip/test", "expected_branch": before["branch"], "expected_head": before["head"]},
            )
            assert "unstage" in error.lower()
            after = await success(session, "get_project_status")
            assert after["branch"] == before["branch"]
        assert await git(tmp_path, "branch", "--list", "wip/test") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["../escape", "-invalid", "wip/../test", "wip//test", "wip/.hidden", "wip/space here"])
def test_branch_creation_rejects_invalid_names(tmp_path: Path, workspace_factory: WorkspaceFactory, name: str) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            error = await failure(
                session,
                "create_project_branch",
                {"new_branch": name, "expected_branch": before["branch"], "expected_head": before["head"]},
            )
            assert "branch" in error.lower()
            after = await success(session, "get_project_status")
            assert after["branch"] == before["branch"]

    asyncio.run(scenario())


def test_branch_creation_rejects_existing_name(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await _seed(tmp_path)
        await git(tmp_path, "branch", "wip/existing")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_status")
            error = await failure(
                session,
                "create_project_branch",
                {"new_branch": "wip/existing", "expected_branch": before["branch"], "expected_head": before["head"]},
            )
            assert "already exists" in error.lower()
            after = await success(session, "get_project_status")
            assert after["branch"] == before["branch"]

    asyncio.run(scenario())
