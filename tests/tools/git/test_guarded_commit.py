"""Guarded commit checks that detect preexisting and mutated staged content."""

import asyncio
from pathlib import Path

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration


def test_guarded_commit_requires_exact_paths_and_observed_index(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "baseline.txt").write_text("baseline")
        await git(tmp_path, "add", "baseline.txt")
        await git(tmp_path, "commit", "-m", "baseline")
        (tmp_path / "intended.txt").write_text("intended")
        (tmp_path / "extra.txt").write_text("unrelated")
        async with client(tmp_path) as session:
            await success(session, "stage_project_paths", {"paths": ["intended.txt", "extra.txt"]})
            observation = await success(session, "get_project_pick_state")
            args = {
                "message": "guarded commit",
                "expected_branch": observation["branch"],
                "expected_head": observation["head"],
                "expected_index_fingerprint": observation["index_fingerprint"],
                "expected_staged_paths": ["intended.txt"],
            }
            old_head = await git(tmp_path, "rev-parse", "--verify", "HEAD")
            assert "staged" in (await failure(session, "commit_project_changes", args)).lower()
            assert await git(tmp_path, "rev-parse", "--verify", "HEAD") == old_head
            assert await git(tmp_path, "ls-files", "--stage")
            await success(session, "unstage_project_paths", {"paths": ["extra.txt"]})
            next_observation = await success(session, "get_project_pick_state")
            args.update(
                expected_index_fingerprint=next_observation["index_fingerprint"],
                expected_staged_paths=["intended.txt"],
            )
            outcome = await success(session, "commit_project_changes", args)
            assert outcome["success"] is True
            assert await git(tmp_path, "show", "HEAD:intended.txt") == "intended"
            assert (tmp_path / "extra.txt").read_text() == "unrelated"
            assert "extra.txt" not in await git(tmp_path, "ls-files")

    asyncio.run(scenario())


def test_guarded_commit_rejects_stale_index_same_paths_and_partial_contract(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "baseline.txt").write_text("baseline")
        await git(tmp_path, "add", "baseline.txt")
        await git(tmp_path, "commit", "-m", "baseline")
        (tmp_path / "file.txt").write_text("first")
        async with client(tmp_path) as session:
            await success(session, "stage_project_paths", {"paths": ["file.txt"]})
            observed = await success(session, "get_project_pick_state")
            args = {
                "message": "guarded",
                "expected_branch": observed["branch"],
                "expected_head": observed["head"],
                "expected_index_fingerprint": observed["index_fingerprint"],
                "expected_staged_paths": ["file.txt"],
            }
            (tmp_path / "file.txt").write_text("second")
            await success(session, "stage_project_paths", {"paths": ["file.txt"]})
            assert "index" in (await failure(session, "commit_project_changes", args)).lower()
            assert (
                "expected"
                in (
                    await failure(
                        session, "commit_project_changes", {"message": "partial", "expected_branch": observed["branch"]}
                    )
                ).lower()
            )
            assert (
                "branch"
                in (await failure(session, "commit_project_changes", {**args, "expected_branch": "other"})).lower()
            )
            assert (
                "HEAD"
                in (await failure(session, "commit_project_changes", {**args, "expected_head": "a" * 40})).upper()
            )
            assert await git(tmp_path, "ls-files", "--stage")
            refreshed = await success(session, "get_project_pick_state")
            args.update(expected_index_fingerprint=refreshed["index_fingerprint"])
            await success(session, "commit_project_changes", args)
            assert await git(tmp_path, "show", "HEAD:file.txt") == "second"

    asyncio.run(scenario())
