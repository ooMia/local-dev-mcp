"""Exercise the public Issue #8 P1/P2 workflow as one real MCP sequence."""

import asyncio
from pathlib import Path

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration


def test_untracked_review_then_guarded_commit_preserves_other_subproject(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "baseline.txt").write_text("baseline\n")
        await git(tmp_path, "add", "baseline.txt")
        await git(tmp_path, "commit", "-m", "baseline")

        server = tmp_path / "apps/server/tests"
        profile = tmp_path / "packages/fde-profile/tests"
        server.mkdir(parents=True)
        profile.mkdir(parents=True)
        first = "apps/server/tests/case.txt"
        second = "packages/fde-profile/tests/case.txt"
        (server / "case.txt").write_text("server-only-marker\n")
        (profile / "case.txt").write_text("profile-only-marker\n")

        async with client(tmp_path) as session:
            initial = await success(session, "get_project_pick_state")
            preview = await success(
                session,
                "get_project_diff",
                {
                    "include_untracked": True,
                    "paths": ["apps/server/tests", "packages/fde-profile/tests"],
                },
            )
            assert preview["complete"] is True
            assert "server-only-marker" in str(preview["patch"])
            assert "profile-only-marker" in str(preview["patch"])
            after_preview = await success(session, "get_project_pick_state")
            assert after_preview["index_fingerprint"] == initial["index_fingerprint"]
            assert after_preview["head"] == initial["head"]

            await success(session, "stage_project_paths", {"paths": [first, second]})
            staged = await success(session, "get_project_diff", {"staged": True})
            assert "server-only-marker" in str(staged["patch"])
            assert "profile-only-marker" in str(staged["patch"])
            observed = await success(session, "get_project_pick_state")

            expected = {
                "message": "commit only server subproject",
                "expected_branch": observed["branch"],
                "expected_head": observed["head"],
                "expected_index_fingerprint": observed["index_fingerprint"],
                "expected_staged_paths": [first],
            }
            denied = await failure(session, "commit_project_changes", expected)
            assert "staged paths" in denied.lower()
            after_denial = await success(session, "get_project_pick_state")
            assert after_denial["head"] == observed["head"]
            assert after_denial["index_fingerprint"] == observed["index_fingerprint"]

            await success(session, "unstage_project_paths", {"paths": [second]})
            reviewed = await success(session, "get_project_diff", {"staged": True})
            assert "server-only-marker" in str(reviewed["patch"])
            assert "profile-only-marker" not in str(reviewed["patch"])
            current = await success(session, "get_project_pick_state")
            assert current["staged_paths"] == [first]
            expected["expected_index_fingerprint"] = current["index_fingerprint"]
            committed = await success(session, "commit_project_changes", expected)
            assert committed["success"] is True
            assert committed["head_before"] == initial["head"]
            assert committed["head_after"] != committed["head_before"]

            final = await success(session, "get_project_pick_state")
            assert final["staged_paths"] == []
            assert isinstance(final["untracked_paths"], list)
            assert second in final["untracked_paths"]

        assert await git(tmp_path, "show", f"HEAD:{first}") == "server-only-marker"
        assert second not in (await git(tmp_path, "ls-files")).splitlines()
        assert (profile / "case.txt").read_text() == "profile-only-marker\n"

    asyncio.run(scenario())
