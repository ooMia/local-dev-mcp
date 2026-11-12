"""Quality evidence binds complete Task Manager Jobs to an exact Worktrunk Git tree."""

import asyncio
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import JobDefinition, Policy
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration

VERIFY = "verify_project_worktrunk_quality"


def _policy(root: Path) -> Policy:
    check = (sys.executable, "-c", "print('quality check passed')")
    return replace(
        policy(root),
        allowed_commands=("git", "wt", "git.worktree.write", "task.check", "task.unit"),
        jobs=(
            JobDefinition("check", "lint", "task.check", check),
            JobDefinition("unit", "test", "task.unit", check, level="unit", default_targets=("base.txt",)),
        ),
    )


async def _fixture(root: Path, factory: WorkspaceFactory) -> str:
    await seed(root, factory)
    (root / ".config").mkdir()
    (root / ".config/quality.toml").write_text(
        'schema_version = 1\n[profiles.pre_merge]\nchecks = ["check", "unit"]\nrequire_clean = true\n'
    )
    (root / "config.toml").write_text("# controller startup recipes are configured by Policy in this test\n")
    await git(root, "add", ".config/quality.toml", "config.toml")
    await git(root, "commit", "-m", "chore: shared checks")
    return await git(root, "rev-parse", "HEAD")


async def _job(client: Client[FastMCPTransport], name: str, base: str, request: str, all_tests: bool) -> str:
    started = await success(
        client,
        "start_project_worktree_task",
        {
            "task": name,
            "request_id": request,
            "branch": "feat/wt-proof",
            "expected_branch_head": base,
            "expected_branch": "main",
            "expected_head": base,
            "backend": "worktrunk",
            "all_tests": all_tests,
        },
    )
    job_id = str(started["job_id"])
    async with asyncio.timeout(15):
        while True:
            result = await success(client, "get_project_task", {"job_id": job_id})
            if result["state"] != "running":
                assert result["state"] == "succeeded", result
                context = cast(dict[str, object], result["worktree"])
                assert context["verified"] is True
                break
            await asyncio.sleep(0.02)
    return job_id


@pytest.mark.skipif(shutil.which("wt") is None, reason="macOS Worktrunk CLI is required")
def test_exact_full_profile_attestation(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base = await _fixture(tmp_path, workspace_factory)
        async with Client(create_server(_policy(tmp_path))) as client:
            created = await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/wt-proof",
                    "expected_branch": "main",
                    "expected_head": base,
                    "expected_base_head": base,
                },
            )
            path = tmp_path / str(created["path"])
            jobs = [
                await _job(client, "check", base, "proof-check", False),
                await _job(client, "unit", base, "proof-unit", True),
            ]
            attestation = await success(
                client,
                VERIFY,
                {"branch": "feat/wt-proof", "expected_source_head": base, "expected_main_head": base, "job_ids": jobs},
            )
            assert attestation["status"] == "VERIFIED"
            assert len(str(attestation["evidence_id"])) > 20
            assert len(str(attestation["source_tree"])) == 40
            assert attestation["source_head"] == base == attestation["target_head"]
            checks = cast(list[dict[str, str]], attestation["checks"])
            assert [entry["name"] for entry in checks] == ["check", "unit"]
            assert path.is_dir()
            assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("wt") is None, reason="macOS Worktrunk CLI is required")
def test_partial_or_stale_quality_jobs_never_issue_evidence(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base = await _fixture(tmp_path, workspace_factory)
        async with Client(create_server(_policy(tmp_path))) as client:
            created = await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/wt-proof",
                    "expected_branch": "main",
                    "expected_head": base,
                    "expected_base_head": base,
                },
            )
            target = tmp_path / str(created["path"])
            check = await _job(client, "check", base, "proof-check", False)
            request: dict[str, object] = {
                "branch": "feat/wt-proof",
                "expected_source_head": base,
                "expected_main_head": base,
                "job_ids": [check],
            }
            assert "one completed Job ID" in await failure(client, VERIFY, request)
            explicit = await success(
                client,
                "start_project_worktree_task",
                {
                    "task": "unit",
                    "request_id": "proof-partial",
                    "branch": "feat/wt-proof",
                    "expected_branch_head": base,
                    "expected_branch": "main",
                    "expected_head": base,
                    "backend": "worktrunk",
                    "targets": ["base.txt"],
                },
            )
            job_id = str(explicit["job_id"])
            async with asyncio.timeout(15):
                while True:
                    item = await success(client, "get_project_task", {"job_id": job_id})
                    if item["state"] != "running":
                        assert item["state"] == "succeeded"
                        break
                    await asyncio.sleep(0.02)
            assert "complete configured target" in await failure(
                client, VERIFY, {**request, "job_ids": [check, job_id]}
            )
            (target / "base.txt").write_text("user mutation\n")
            assert "dirty" in await failure(client, VERIFY, {**request, "job_ids": [check, job_id]})
            assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())
