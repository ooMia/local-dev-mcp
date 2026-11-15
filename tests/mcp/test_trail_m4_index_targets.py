"""M4 internal Git-registered index target isolation and real Trail lookup tests."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager
from local_dev_mcp.tools.indexing._targets import resolve_index_target
from tests.support.git import WorkspaceFactory, git
from tests.support.worktrees import seed

pytestmark = pytest.mark.integration


def _grep(trail: str, query: str, root: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(
        [trail, "grep", query, str(root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=environment,
    )
    return result.stdout


def _query(trail: str, verb: str, arg: str, root: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(
        [trail, verb, arg, str(root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=environment,
    )
    return result.stdout


def test_registered_worktree_targets_are_separate_and_root_explicit(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    trail = shutil.which("trail", path=os.environ.get("PATH"))
    if not trail:
        pytest.skip("Real Trail CLI is required for M4 cross-branch query evidence.")

    async def scenario() -> None:
        await seed(tmp_path, workspace_factory)
        (tmp_path / ".gitignore").write_text(".lab/\n.env\n/graft/\n", encoding="utf-8")
        (tmp_path / "module.py").write_text(
            'def marker() -> str:\n    return "M4_ONLY_MAIN_71"\n\ndef call_main() -> str:\n    return marker()\n',
            encoding="utf-8",
        )
        await git(tmp_path, "add", ".gitignore", "module.py")
        await git(tmp_path, "commit", "-m", "test: add main marker")
        original_head = await git(tmp_path, "rev-parse", "HEAD")

        nested = tmp_path / ".lab" / "wt" / "feature"
        await git(tmp_path, "worktree", "add", "-b", "feat/m4-target", str(nested), "main")
        (nested / "module.py").write_text(
            'def marker() -> str:\n    return "M4_ONLY_FEATURE_89"\n'
            "\ndef call_feature() -> str:\n    return marker()\n",
            encoding="utf-8",
        )
        await git(nested, "commit", "-am", "test: diverge feature marker")
        feature_head = await git(nested, "rev-parse", "HEAD")

        policy = Policy(tmp_path, (tmp_path,), ("git", "wt", "git.worktree.write", "trail"))
        settings = ServerSettings.load(policy)
        selected = await resolve_index_target(
            settings, kind="worktree", branch="feat/m4-target", expected_head=feature_head
        )
        root = await resolve_index_target(settings, kind="project")
        assert selected.policy.root == nested
        assert root.policy.root == tmp_path
        assert settings.policy.root == tmp_path
        assert dict(selected.environment)["GRAFT_NO_IGNORE"] == "1"
        assert dict(selected.environment)["GRAFT_NO_GITIGNORE"] == "1"
        assert dict(root.environment).get("GRAFT_NO_IGNORE") is None
        # Existing M1-M3 project settings are intentionally unchanged. The
        # disposable main-checkout probe explicitly suppresses Trail artifacts.
        main_environment = dict(root.environment)
        main_environment.update({"GRAFT_NO_IGNORE": "1", "GRAFT_NO_GITIGNORE": "1"})
        root_index = IndexManager(root.policy, main_environment, executable=trail)

        with pytest.raises(ToolError):
            await resolve_index_target(settings, kind="project", branch="feat/m4-target")
        with pytest.raises(ToolError):
            await resolve_index_target(settings, kind="worktree", branch="feat/m4-target")
        with pytest.raises(ToolError):
            await resolve_index_target(settings, kind="worktree", branch="feat/m4-target", expected_head="f" * 40)
        restricted = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git", "wt", "git.worktree.write")))
        with pytest.raises(ToolError):
            await resolve_index_target(restricted, kind="project")

        try:
            # Each IndexManager pins its own explicit root; never use Trail's
            # nearest ancestor inference for a nested linked checkout.
            for index in (root_index, selected.index):
                result = await index.refresh()
                assert result["structural_state"] == "fresh", result
                assert result["ready_for_structural_search"], result
            main_result = _grep(trail, "M4_ONLY_MAIN_71", tmp_path, main_environment)
            feature_result = _grep(trail, "M4_ONLY_FEATURE_89", nested, dict(selected.environment))
            assert "M4_ONLY_MAIN_71" in main_result
            assert "M4_ONLY_FEATURE_89" in feature_result
            assert "M4_ONLY_FEATURE_89" not in _grep(trail, "M4_ONLY_FEATURE_89", tmp_path, main_environment)
            assert "M4_ONLY_MAIN_71" not in _grep(trail, "M4_ONLY_MAIN_71", nested, dict(selected.environment))
            # Signatures and call edges must come from the exact checkout root,
            # not the nearest ancestor repository's graph.
            main_skeleton = _query(trail, "skeleton", "module.py", tmp_path, main_environment)
            feature_skeleton = _query(trail, "skeleton", "module.py", nested, dict(selected.environment))
            assert "call_main" in main_skeleton and "call_feature" not in main_skeleton
            assert "call_feature" in feature_skeleton and "call_main" not in feature_skeleton
            main_callers = _query(trail, "callers", "marker", tmp_path, main_environment)
            feature_callers = _query(trail, "callers", "marker", nested, dict(selected.environment))
            assert "call_main" in main_callers and "call_feature" not in main_callers
            assert "call_feature" in feature_callers and "call_main" not in feature_callers
            assert await git(tmp_path, "status", "--porcelain") == ""
            assert await git(nested, "status", "--porcelain") == ""
            assert await git(tmp_path, "rev-parse", "HEAD") == original_head
            assert await git(nested, "rev-parse", "HEAD") == feature_head
            (nested / "module.py").write_text("uncommitted user change\n", encoding="utf-8")
            with pytest.raises(ToolError):
                await resolve_index_target(
                    settings, kind="worktree", branch="feat/m4-target", expected_head=feature_head
                )
            assert (nested / "module.py").read_text() == "uncommitted user change\n"
        finally:
            await selected.index.close()
            await root_index.close()

    asyncio.run(scenario())
