"""Opt-in integration requires assessment and creates one verified squash commit."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success
from tests.support.release import feature, release_workspace
from tests.support.signing import SigningSetup

pytestmark = pytest.mark.integration


def merge_args(base: str, head: str, *, message: str | None = "feat(mcp): add example") -> dict[str, object]:
    values: dict[str, object] = {
        "expected_source_branch": "feat/example",
        "expected_source_head": head,
        "expected_main_head": base,
    }
    if message is not None:
        values["message"] = message
    return values


def test_assessed_integration_squashes_two_source_commits_without_version_bump(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        await feature(tmp_path)
        (tmp_path / "second.txt").write_text("followup\n")
        await git(tmp_path, "add", "second.txt")
        await git(tmp_path, "commit", "-m", "fix: followup")
        head = await git(tmp_path, "rev-parse", "HEAD")
        tree = await git(tmp_path, "rev-parse", "HEAD^{tree}")
        async with client(tmp_path) as session:
            result = await success(session, "merge_project_branch", merge_args(base, head))
            assert result["mode"] == "squash"
            assert result["assessments"] == ["example"]
            assert result["main_head_after"] not in (head, base)
            assert (await success(session, "get_project_status"))["entries"] == []
        assert await git(tmp_path, "rev-parse", "HEAD^{tree}") == tree
        assert await git(tmp_path, "rev-parse", "HEAD^") == base
        assert await git(tmp_path, "rev-list", "--count", f"{base}..main") == "1"
        assert 'version = "1.2.3"' in (tmp_path / "pyproject.toml").read_text()
        assert await git(tmp_path, "rev-parse", "feat/example") == head

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["missing-record", "missing-message", "bad-message", "bad-branch"])
def test_invalid_integration_fails_before_switch_or_main_update(
    tmp_path: Path, workspace_factory: WorkspaceFactory, invalid: str
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        head = await feature(tmp_path, assessment=invalid != "missing-record")
        args = merge_args(base, head, message=None if invalid == "missing-message" else "feat: add example")
        branch = "feat/example"
        if invalid == "bad-message":
            args["message"] = "temporary checkpoint"
        if invalid == "bad-branch":
            branch = "migration/example"
            await git(tmp_path, "branch", "-m", branch)
            args["expected_source_branch"] = branch
        async with client(tmp_path) as session:
            assert await failure(session, "merge_project_branch", args)
            assert (await success(session, "get_project_status"))["branch"] == branch
        assert await git(tmp_path, "rev-parse", "main") == base

    asyncio.run(scenario())


def test_restricted_inherited_assessment_is_not_read_or_merged(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory, pending=True)
        head = await feature(tmp_path)
        restricted = tmp_path / ".changes/previous.toml"
        policy = Policy(tmp_path, (tmp_path,), ("git",), denied_paths=(restricted,))
        async with Client(create_server(policy)) as session:
            error = await failure(session, "merge_project_branch", merge_args(base, head))
            assert "Existing public calls" not in error
            assert (await success(session, "get_project_status"))["branch"] == "feat/example"
        assert await git(tmp_path, "rev-parse", "main") == base

    asyncio.run(scenario())


def test_squash_signs_new_commit_instead_of_requiring_source_signature(
    tmp_path: Path, workspace_factory: WorkspaceFactory, signing_setup: SigningSetup
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        head = await feature(tmp_path)
        with (tmp_path / ".env").open("a") as stream:
            stream.write(signing_setup.dotenv())
        async with client(tmp_path, signing=signing_setup) as session:
            result = await success(session, "merge_project_branch", merge_args(base, head))
            assert result["signature_verified"] is True
            assert result["main_head_after"] != head
        assert await git(tmp_path, "rev-parse", "HEAD^") == base

    asyncio.run(scenario())


@pytest.mark.parametrize("hook", ["reject", "change-message"])
def test_hook_failure_or_changed_message_reports_partial_state_without_replay(
    tmp_path: Path, workspace_factory: WorkspaceFactory, hook: str
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        head = await feature(tmp_path)
        path = tmp_path / ".git/hooks" / ("pre-commit" if hook == "reject" else "commit-msg")
        path.write_text("#!/bin/sh\nexit 1\n" if hook == "reject" else '#!/bin/sh\nprintf "invalid title\\n" > "$1"\n')
        path.chmod(0o700)
        async with client(tmp_path) as session:
            error = await failure(session, "merge_project_branch", merge_args(base, head))
            assert "inspect" in error.lower()
            observed = await success(session, "get_project_status")
            assert observed["branch"] == "main"
            if hook == "reject":
                assert observed["head"] == base
                assert observed["entries"]
            else:
                assert observed["head"] not in (base, head)
                assert observed["entries"] == []
        assert await git(tmp_path, "rev-parse", "feat/example") == head

    asyncio.run(scenario())


def test_release_version_branch_can_be_created_and_resolved_by_existing_tools(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        async with client(tmp_path) as session:
            result = await success(
                session,
                "create_project_branch",
                {"new_branch": "release/1.3.0", "expected_branch": "main", "expected_head": base},
            )
            assert result["branch_after"] == "release/1.3.0"
            assert (await success(session, "get_project_ref", {"ref": "release/1.3.0"}))["head"] == base
            assert await failure(
                session,
                "create_project_branch",
                {"new_branch": "release/01.3.0", "expected_branch": "release/1.3.0", "expected_head": base},
            )

    asyncio.run(scenario())


def test_unconfigured_symlink_manifest_preserves_legacy_fast_forward(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        await git(tmp_path, "branch", "-M", "main")
        (tmp_path / "manifest.toml").write_text('[project]\nname = "legacy"\nversion = "1.0.0"\n')
        (tmp_path / "pyproject.toml").symlink_to("manifest.toml")
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "legacy project")
        base = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "-c", "feat/example")
        (tmp_path / "feature.txt").write_text("legacy feature\n")
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "legacy feature")
        head = await git(tmp_path, "rev-parse", "HEAD")
        async with client(tmp_path) as session:
            result = await success(session, "merge_project_branch", merge_args(base, head, message=None))
            assert result["mode"] == "fast-forward"
            assert result["main_head_after"] == head

    asyncio.run(scenario())
