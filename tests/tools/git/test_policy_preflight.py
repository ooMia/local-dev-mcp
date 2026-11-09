"""Policy preflight must expose integration blockers without mutating Git state."""

import asyncio
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success
from tests.support.release import feature, release_workspace

pytestmark = pytest.mark.integration


def args(base: str, head: str, source_ref: str = "feat/example") -> dict[str, object]:
    return {
        "operation": "integrate_main",
        "source_ref": source_ref,
        "expected_main_head": base,
        "expected_source_head": head,
    }


def check_states(report: dict[str, object]) -> dict[str, object]:
    raw = report["checks"]
    assert isinstance(raw, list)
    states: dict[str, object] = {}
    for item in cast(list[object], raw):
        assert isinstance(item, dict)
        entry = cast(dict[str, object], item)
        code = entry.get("code")
        assert isinstance(code, str)
        states[code] = entry.get("state")
    return states


def test_preflight_reports_external_review_after_valid_local_assessment(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        head = await feature(tmp_path)
        before = await git(tmp_path, "status", "--porcelain")
        async with client(tmp_path) as session:
            report = await success(session, "inspect_project_policy", args(base, head))
            assert report["state"] == "external_review_required"
            gates = check_states(report)
            assert gates == {
                "topic_branch": "pass",
                "main_ancestry": "pass",
                "release_assessment": "pass",
                "pr_quality_and_provenance": "pending",
            }
            references = report["policy_references"]
            assert isinstance(references, list)
            assert "docs/tools/workflow/README.md" in references
            assert report["scope"] == "local_preflight_only"
        assert await git(tmp_path, "status", "--porcelain") == before
        assert await git(tmp_path, "rev-parse", "main") == base

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["no-assessment", "wrong-branch", "denied-assessment"])
def test_preflight_blocks_invalid_local_source_without_writing(
    tmp_path: Path, workspace_factory: WorkspaceFactory, invalid: str
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        head = await feature(tmp_path, assessment=invalid != "no-assessment")
        branch = "feat/example"
        if invalid == "wrong-branch":
            branch = "consolidated/example"
            await git(tmp_path, "branch", "-m", branch)
        selection = (
            Policy(
                tmp_path,
                (tmp_path,),
                ("git",),
                denied_paths=(tmp_path / ".changes/example.toml",),
            )
            if invalid == "denied-assessment"
            else None
        )
        async with client(tmp_path) if selection is None else Client(create_server(selection)) as session:
            report = await success(session, "inspect_project_policy", args(base, head, branch))
            assert report["state"] == "blocked"
            gates = check_states(report)
            assert gates["topic_branch" if invalid == "wrong-branch" else "release_assessment"] == "blocked"
        assert await git(tmp_path, "rev-parse", "main") == base
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_preflight_rejects_stale_observed_source(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        await feature(tmp_path)
        async with client(tmp_path) as session:
            await failure(session, "inspect_project_policy", args(base, base))

    asyncio.run(scenario())
