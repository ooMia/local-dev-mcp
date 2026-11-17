"""Release planning and preparation through the real MCP boundary."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client
from pydantic import TypeAdapter

from local_dev_mcp.execution import base_environment
from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.get_tool_readiness import ReadinessReport
from local_dev_mcp.tools.release import read_snapshot
from local_dev_mcp.tools.release._git import LocalGit
from local_dev_mcp.tools.release.mcp_tools import ReleaseSummary
from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success
from tests.support.release import record_text, release_workspace

pytestmark = pytest.mark.integration


def test_release_plan_exposes_committed_assessments_without_writing(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head = await release_workspace(tmp_path, workspace_factory, pending=True)
        original = (tmp_path / ".changes/previous.toml").read_bytes()
        async with client(tmp_path) as session:
            result = await success(session, "get_release_plan")
            assert result["branch"] == "main"
            assert result["clean"] is True
            plan = result["plan"]
            assert isinstance(plan, dict)
            assert plan["head"] == head
            assert plan["current_version"] == "1.2.3"
            assert plan["next_version"] == "1.3.0"
            assert plan["maximum_impact"] == "minor"
            assert plan["causes"] == ["previous"]
            assert plan["changes"][0]["reason"] == "Existing public calls remain valid"
            # A dirty local evaluation must not silently replace the committed one.
            (tmp_path / ".changes/previous.toml").write_text(record_text(impact="major"))
            dirty = await success(session, "get_release_plan")
            assert dirty["clean"] is False
            assert dirty["plan"] == plan
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert original != (tmp_path / ".changes/previous.toml").read_bytes()

    asyncio.run(scenario())


async def prepared_workspace(root: Path, workspace_factory: WorkspaceFactory) -> tuple[str, str]:
    await release_workspace(root, workspace_factory, pending=True)
    await asyncio.to_thread(
        subprocess.run,
        ["uv", "lock", "--offline", "--no-config", "--cache-dir", str(root.parent / "uv-cache")],
        cwd=root,
        env=base_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=30,
    )
    await git(root, "add", "uv.lock")
    await git(root, "commit", "-m", "build: initial lock")
    head = await git(root, "rev-parse", "HEAD")
    digest = (await read_snapshot(LocalGit(root).read, head)).plan()["digest"]
    return head, digest


def test_chat_release_prepare_updates_version_and_consumes_assessments(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, digest = await prepared_workspace(tmp_path, workspace_factory)
        async with client(tmp_path, commands=("git", "uv.release")) as session:
            listed = {tool.name: tool for tool in await session.list_tools()}
            assert listed["get_release_plan"].input_schema["properties"] == {}
            assert set(listed["prepare_project_release"].input_schema["required"]) == {
                "expected_head",
                "expected_plan",
                "expected_version",
            }
            plan = TypeAdapter(ReleaseSummary).validate_python(await success(session, "get_release_plan"))
            assert plan["plan"]["digest"] == digest
            assert plan["plan"]["next_version"] == "1.3.0"
            assert "uv.lock" in await git(tmp_path, "ls-files")

            # The caller first chooses the recommended release branch.
            await success(
                session,
                "create_project_branch",
                {"new_branch": "release/1.3.0", "expected_branch": "main", "expected_head": head},
            )
            assert "Release HEAD, plan digest or version changed" in await failure(
                session,
                "prepare_project_release",
                {"expected_head": head, "expected_plan": "0" * 64, "expected_version": "1.3.0"},
            )
            assert "Release HEAD, plan digest or version changed" in await failure(
                session,
                "prepare_project_release",
                {"expected_head": head, "expected_plan": digest, "expected_version": "1.3.1"},
            )
            assert (tmp_path / ".changes/previous.toml").exists()

            prepared = await success(
                session,
                "prepare_project_release",
                {"expected_head": head, "expected_plan": digest, "expected_version": "1.3.0"},
            )
            assert prepared["prepared"] is True
            assert prepared["version"] == "1.3.0"
            assert prepared["commit_created"] is False
            assert prepared["branch"] == "release/1.3.0"
            assert not (tmp_path / ".changes/previous.toml").exists()
            assert "1.3.0" in (tmp_path / "pyproject.toml").read_text()
            assert "1.3.0" in (tmp_path / "uv.lock").read_text()
            assert (tmp_path / "CHANGELOG.md").exists()
            assert (tmp_path / ".release.toml").exists()
            assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())


def test_prepare_requires_opt_in_permission_and_respects_read_only_policy(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, digest = await prepared_workspace(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "release/1.3.0")
        args: dict[str, object] = {"expected_head": head, "expected_plan": digest, "expected_version": "1.3.0"}
        async with client(tmp_path) as session:
            report = TypeAdapter(ReadinessReport).validate_python(await success(session, "get_tool_readiness"))
            preparation = next(x for x in report["tools"] if x["name"] == "prepare_project_release")
            assert preparation["ready"] is False
            assert "uv.release" in await failure(session, "prepare_project_release", args)
        policy = Policy(tmp_path, (tmp_path,), ("git", "uv.release"), (tmp_path / "uv.lock",))
        async with Client(create_server(policy)) as session:
            assert "not writable" in await failure(session, "prepare_project_release", args)
        assert not (tmp_path / ".release.toml").exists()
        assert (tmp_path / ".changes/previous.toml").exists()
        assert 'version = "1.2.3"' in (tmp_path / "pyproject.toml").read_text()

    asyncio.run(scenario())


def test_prepare_denies_dirty_or_unexpected_branch(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, digest = await prepared_workspace(tmp_path, workspace_factory)
        args: dict[str, object] = {"expected_head": head, "expected_plan": digest, "expected_version": "1.3.0"}
        async with client(tmp_path, commands=("git", "uv.release")) as session:
            assert "clean" in await failure(session, "prepare_project_release", args)
            await git(tmp_path, "switch", "-c", "release/1.3.0")
            (tmp_path / "untracked").write_text("extra")
            assert "clean" in await failure(session, "prepare_project_release", args)
            assert not (tmp_path / ".release.toml").exists()

    asyncio.run(scenario())


def test_plan_does_not_expose_denied_assessment_content(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await release_workspace(tmp_path, workspace_factory, pending=True)
        policy = Policy(tmp_path, (tmp_path,), ("git",), denied_paths=(tmp_path / ".changes",))
        async with Client(create_server(policy)) as session:
            error = await failure(session, "get_release_plan")
            assert "unavailable or invalid" in error
            assert "Existing public calls remain valid" not in error

    asyncio.run(scenario())
