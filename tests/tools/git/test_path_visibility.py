"""Keep protected and unreadable paths out of public Git results and writes."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, client, git, initialize
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration

CANARY = "synthetic-private-value-must-not-reach-MCP"


def test_protected_tracked_and_staged_content_is_filtered_before_patch(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "public.txt").write_text("public before\n")
        (tmp_path / ".env.local").write_text("SECRET=private-before\n")
        await git(tmp_path, "add", "-f", "--", "public.txt", ".env.local")
        await git(tmp_path, "-c", "commit.gpgSign=false", "commit", "-qm", "baseline")
        (tmp_path / "public.txt").write_text("public after\n")
        (tmp_path / ".env.local").write_text(f"SECRET={CANARY}\n")

        async with client(tmp_path) as session:
            status = await success(session, "get_project_status")
            assert "public.txt" in str(status["entries"])
            assert ".env.local" not in str(status["entries"])
            assert status["complete"] is False and status["reason"] == "restricted_paths"
            unstaged = await success(session, "get_project_diff")
            assert "+public after" in str(unstaged["patch"])
            assert CANARY not in str(unstaged) and ".env.local" not in str(unstaged["patch"])
            assert unstaged["complete"] is False
            error = await failure(session, "get_project_diff", {"paths": [".env.local"]})
            assert "protected" in error and CANARY not in error
            await git(tmp_path, "add", "-f", "--", "public.txt", ".env.local")
            staged = await success(session, "get_project_diff", {"staged": True})
            assert "+public after" in str(staged["patch"])
            assert CANARY not in str(staged)
            assert staged["complete"] is False
            public = await success(session, "get_project_diff", {"staged": True, "paths": ["public.txt"]})
            assert public["complete"] is True and public["reason"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("restriction", ["denied", "allowed_subtree"])
def test_git_results_respect_path_policy_and_do_not_expose_hidden_names(
    tmp_path: Path, workspace_factory: WorkspaceFactory, restriction: str
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        public = tmp_path / "public"
        public.mkdir()
        (public / "code.py").write_text("before\n")
        private = tmp_path / "private-key.json"
        private.write_text("before\n")
        await git(tmp_path, "add", "--", "public", private.name)
        await git(tmp_path, "-c", "commit.gpgSign=false", "commit", "-qm", "baseline")
        (public / "code.py").write_text("after\n")
        private.write_text(CANARY)
        policy = Policy(
            tmp_path,
            (public,) if restriction == "allowed_subtree" else (tmp_path,),
            ("git",),
            denied_paths=(private,) if restriction == "denied" else (),
        )
        async with Client(create_server(policy)) as session:
            result = await success(session, "get_project_diff")
            assert "+after" in str(result["patch"])
            assert result["complete"] is False
            assert CANARY not in str(result) and private.name not in str(result)
            status = await success(session, "get_project_status")
            assert status["complete"] is False and private.name not in str(status)
            error = await failure(session, "get_project_diff", {"paths": [private.name]})
            assert "protected or outside" in error and CANARY not in error

    asyncio.run(scenario())


@pytest.mark.parametrize("old,new", [(".env.local", "visible.txt"), ("visible.txt", ".env.local")])
def test_rename_with_a_protected_side_never_leaks_the_other_side(
    tmp_path: Path, workspace_factory: WorkspaceFactory, old: str, new: str
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / old).write_text(CANARY + "\n")
        await git(tmp_path, "add", "-f", "--", old)
        await git(tmp_path, "-c", "commit.gpgSign=false", "commit", "-qm", "baseline")
        await git(tmp_path, "mv", "--", old, new)
        async with client(tmp_path) as session:
            result = await success(session, "get_project_diff", {"staged": True})
            assert result["patch"] == "" and result["complete"] is False
            assert CANARY not in str(result)
            status = await success(session, "get_project_status")
            assert status["entries"] == [] and status["complete"] is False
            allowed_side = new if old == ".env.local" else old
            selected = await success(session, "get_project_diff", {"staged": True, "paths": [allowed_side]})
            assert selected["patch"] == "" and selected["complete"] is False
            assert selected["reason"] == "restricted_paths"
            assert CANARY not in str(selected)

    asyncio.run(scenario())


def test_stage_and_commit_rejections_preserve_index_and_head(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        private = tmp_path / "private.json"
        private.write_text(CANARY)
        (tmp_path / "public.txt").write_text("public\n")
        policy = Policy(tmp_path, (tmp_path,), ("git",), denied_paths=(private,))
        async with Client(create_server(policy)) as session:
            error = await failure(session, "stage_project_paths", {"paths": ["."]})
            assert "No staging was attempted" in error and CANARY not in error
            assert await git(tmp_path, "ls-files") == ""
            await success(session, "stage_project_paths", {"paths": ["public.txt"]})
            await git(tmp_path, "add", "--", private.name)
            before = await git(tmp_path, "write-tree")
            error = await failure(session, "commit_project_changes", {"message": "must be rejected"})
            assert "No commit was attempted" in error and CANARY not in error
            assert await git(tmp_path, "write-tree") == before
            assert (await success(session, "get_project_status"))["head"] is None
            # Removing an accidentally staged private entry is a safe recovery action.
            await success(session, "unstage_project_paths", {"paths": [private.name]})
            result = await success(session, "commit_project_changes", {"message": "public only"})
            assert result["success"] is True

    asyncio.run(scenario())


def test_read_only_scope_does_not_block_predefined_git_stage_and_commit(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        file = tmp_path / "readonly.txt"
        file.write_text("user or formatter change\n")
        policy = Policy(tmp_path, (tmp_path,), ("git",), read_only_paths=(file,))
        async with Client(create_server(policy)) as session:
            await success(session, "stage_project_paths", {"paths": [file.name]})
            result = await success(session, "commit_project_changes", {"message": "record allowed side effect"})
            assert result["success"] is True
            assert (await success(session, "get_project_status"))["complete"] is True

    asyncio.run(scenario())


def test_repository_submodule_diff_configuration_cannot_expand_private_nested_content(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        module = tmp_path / "module"
        module.mkdir()
        await initialize(module)
        private = module / ".env.local"
        private.write_text("before\n")
        await git(module, "add", "-f", "--", private.name)
        await git(module, "-c", "commit.gpgSign=false", "commit", "-qm", "inner baseline")
        head = await git(module, "rev-parse", "HEAD")
        await git(tmp_path, "update-index", "--add", "--cacheinfo", f"160000,{head},module")
        await git(tmp_path, "-c", "commit.gpgSign=false", "commit", "-qm", "outer baseline")
        private.write_text(CANARY + "\n")
        await git(module, "add", "-f", "--", private.name)
        await git(module, "-c", "commit.gpgSign=false", "commit", "-qm", "inner change")
        await git(tmp_path, "config", "diff.submodule", "diff")
        assert CANARY in await git(tmp_path, "diff", "--", "module")
        async with client(tmp_path) as session:
            result = await success(session, "get_project_diff")
            assert "Subproject commit" in str(result["patch"])
            assert CANARY not in str(result) and ".env.local" not in str(result)
            assert result["complete"] is True

    asyncio.run(scenario())
