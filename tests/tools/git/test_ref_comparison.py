"""Local ref comparison: immutable baselines, direction, visibility, and no writes."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration

CANARY = "synthetic-private-value-must-never-be-returned"


async def fixture_history(root: Path, workspace_factory: WorkspaceFactory) -> tuple[str, str]:
    await workspace_factory()
    await git(root, "branch", "-M", "main")
    (root / "public.txt").write_text("base\n")
    await git(root, "add", "public.txt")
    await git(root, "commit", "-m", "base")
    main = await git(root, "rev-parse", "HEAD")
    await git(root, "switch", "-c", "wip/github-automation-20261030")
    (root / "public.txt").write_text("target\n")
    await git(root, "add", "public.txt")
    await git(root, "commit", "-m", "target")
    return main, await git(root, "rev-parse", "HEAD")


def comparison(base: str, target: str) -> dict[str, object]:
    return {
        "base_ref": "main",
        "target_ref": "wip/github-automation-20261030",
        "expected_base_head": base,
        "expected_target_head": target,
    }


def test_compare_local_branches_reports_direction_without_switching(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, target = await fixture_history(tmp_path, workspace_factory)
        async with client(tmp_path) as session:
            first = await success(session, "get_project_ref", {"ref": "main"})
            second = await success(session, "get_project_ref", {"ref": "wip/github-automation-20261030"})
            assert first["head"] == base and second["head"] == target
            assert (await success(session, "get_project_ref", {"ref": target}))["head"] == target

            before = await success(session, "get_project_status")
            result = await success(session, "compare_project_refs", comparison(base, target))
            after = await success(session, "get_project_status")
            assert before["branch"] == after["branch"] == "wip/github-automation-20261030"
            assert before["head"] == after["head"] == target
            assert before["entries"] == after["entries"] == []
            assert result["merge_base"] == base
            assert result["base_only_commits"] == 0
            assert result["target_only_commits"] == 1
            assert result["complete"] is True
            assert "+target" in str(result["patch"])
            assert "-base" in str(result["patch"])
            assert "public.txt" in str(result["changes"])
            assert (
                await success(
                    session, "compare_project_refs", {**comparison(base, target), "paths": ["nonexistent.txt"]}
                )
            )["patch"] == ""

    asyncio.run(scenario())


def test_ref_comparison_rejects_stale_refs_and_expression_injection(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        main, feature = await fixture_history(tmp_path, workspace_factory)
        async with client(tmp_path) as session:
            for ref in ("HEAD~1", "--upload-pack", "refs/heads/main", "../main", "main^{tree}", "a" * 130):
                assert await failure(session, "get_project_ref", {"ref": ref})
            assert await failure(session, "get_project_ref", {"ref": "no-such-branch"})
            for bad in (("f" * 40, feature), (main, "f" * 40)):
                assert (
                    "re-observe"
                    in (
                        await failure(
                            session,
                            "compare_project_refs",
                            {
                                **comparison(main, feature),
                                "expected_base_head": bad[0],
                                "expected_target_head": bad[1],
                            },
                        )
                    ).lower()
                )
            assert await failure(
                session, "compare_project_refs", {**comparison(main, feature), "paths": ["../outside"]}
            )
        assert await git(tmp_path, "rev-parse", "refs/heads/main") == main

    asyncio.run(scenario())


def test_compare_reports_diverged_histories_without_merging(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        main_base, feature = await fixture_history(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "main")
        (tmp_path / "main-only.txt").write_text("main extra\n")
        await git(tmp_path, "add", "main-only.txt")
        await git(tmp_path, "commit", "-m", "main divergence")
        main = await git(tmp_path, "rev-parse", "HEAD")
        async with client(tmp_path) as session:
            result = await success(session, "compare_project_refs", comparison(main, feature))
            assert result["merge_base"] == main_base
            assert result["base_only_commits"] == 1
            assert result["target_only_commits"] == 1
            assert result["complete"] is True
            assert (await success(session, "get_project_status"))["branch"] == "main"
        assert await git(tmp_path, "rev-parse", "main") == main

    asyncio.run(scenario())


def test_compare_hides_protected_rename_endpoints_and_patch_contents(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        main, _ = await fixture_history(tmp_path, workspace_factory)
        (tmp_path / ".env.local").write_text(CANARY + "\n")
        await git(tmp_path, "add", "-f", ".env.local")
        await git(tmp_path, "commit", "-m", "private")
        target = await git(tmp_path, "rev-parse", "HEAD")
        async with client(tmp_path) as session:
            result = await success(session, "compare_project_refs", comparison(main, target))
            assert result["complete"] is False and result["reason"] == "restricted_paths"
            assert "+target" in str(result["patch"])
            assert CANARY not in str(result) and ".env.local" not in str(result)
            assert "public.txt" in str(result["changes"])
            assert await failure(session, "compare_project_refs", {**comparison(main, target), "paths": [".env.local"]})
        await git(tmp_path, "mv", ".env.local", "visible-renamed.txt")
        rename_sha = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "commit", "-m", "rename")
        target = await git(tmp_path, "rev-parse", "HEAD")
        # Compare adjacent commits: one endpoint is a protected path.
        async with client(tmp_path) as session:
            restricted = await success(
                session,
                "compare_project_refs",
                {
                    **comparison(main, target),
                    "base_ref": rename_sha,
                    "expected_base_head": rename_sha,
                },
            )
            assert restricted["complete"] is False
            assert CANARY not in str(restricted)
            assert "visible-renamed.txt" not in str(restricted["changes"])

    asyncio.run(scenario())


def test_compare_denied_path_is_filtered_even_if_other_file_readable(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        main, _ = await fixture_history(tmp_path, workspace_factory)
        hidden = tmp_path / "hidden.txt"
        hidden.write_text(CANARY)
        await git(tmp_path, "add", "hidden.txt")
        await git(tmp_path, "commit", "-m", "hidden")
        new_head = await git(tmp_path, "rev-parse", "HEAD")
        policy = Policy(tmp_path, (tmp_path,), ("git",), denied_paths=(hidden,))
        async with Client(create_server(policy)) as session:
            result = await success(session, "compare_project_refs", comparison(main, new_head))
            assert result["complete"] is False
            assert CANARY not in str(result) and "hidden.txt" not in str(result)
            assert "public.txt" in str(result["changes"])
        assert await git(tmp_path, "rev-parse", "HEAD") == new_head

    asyncio.run(scenario())
