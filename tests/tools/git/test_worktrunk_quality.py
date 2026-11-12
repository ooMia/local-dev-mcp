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


async def _job(
    client: Client[FastMCPTransport],
    name: str,
    base: str,
    request: str,
    all_tests: bool,
    source: str | None = None,
    original_branch: str = "main",
) -> str:
    started = await success(
        client,
        "start_project_worktree_task",
        {
            "task": name,
            "request_id": request,
            "branch": "feat/wt-proof",
            "expected_branch_head": source or base,
            "expected_branch": original_branch,
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
            wrong_order = await failure(
                client,
                VERIFY,
                {
                    "branch": "feat/wt-proof",
                    "expected_source_head": base,
                    "expected_main_head": base,
                    "job_ids": list(reversed(jobs)),
                },
            )
            assert "did not pass" in wrong_order
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
        async with Client(create_server(_policy(tmp_path))) as restarted:
            missing = await failure(
                restarted,
                "get_project_worktrunk_merge_plan",
                {
                    "evidence_id": attestation["evidence_id"],
                    "branch": "feat/wt-proof",
                    "expected_source_head": base,
                    "expected_main_head": base,
                },
            )
            assert "unknown or expired" in missing

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


@pytest.mark.skipif(shutil.which("wt") is None, reason="macOS Worktrunk CLI is required")
def test_merge_plan_rechecks_evidence_and_does_not_write_main(
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
            (target / "feature.txt").write_text("feature\n")
            await git(target, "add", "feature.txt")
            await git(target, "commit", "-m", "feat: plan-only")
            source = await git(target, "rev-parse", "HEAD")
            ids = [
                await _job(client, "check", base, "plan-check", False, source),
                await _job(client, "unit", base, "plan-unit", True, source),
            ]
            verified = await success(
                client,
                VERIFY,
                {"branch": "feat/wt-proof", "expected_source_head": source, "expected_main_head": base, "job_ids": ids},
            )
            arguments = {
                "evidence_id": verified["evidence_id"],
                "branch": "feat/wt-proof",
                "expected_source_head": source,
                "expected_main_head": base,
            }
            plan = await success(client, "get_project_worktrunk_merge_plan", arguments)
            assert plan["status"] == "approval_required"
            assert plan["mode"] == "ff"  # The reviewed operation must be explicit, never an implicit squash.
            assert plan["write_requested"] is False
            assert plan["source_head"] == source
            assert plan["target_head"] == base
            assert len(str(plan["plan_digest"])) == 64
            assert await git(tmp_path, "rev-parse", "HEAD") == base
            assert "unknown or expired" in await failure(
                client, "get_project_worktrunk_merge_plan", {**arguments, "evidence_id": "untrusted"}
            )
            (tmp_path / "base.txt").write_text("dirty main\n")
            unchanged_plan = await success(client, "get_project_worktrunk_merge_plan", arguments)
            assert unchanged_plan["plan_digest"] == plan["plan_digest"]
            assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("wt") is None, reason="macOS Worktrunk CLI is required")
def test_quality_jobs_from_different_controller_checkouts_are_rejected(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base = await _fixture(tmp_path, workspace_factory)
        async with Client(create_server(_policy(tmp_path))) as client:
            await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/wt-proof",
                    "expected_branch": "main",
                    "expected_head": base,
                    "expected_base_head": base,
                },
            )
            check = await _job(client, "check", base, "mixed-main", False)
            await git(tmp_path, "branch", "other", base)
            await git(tmp_path, "switch", "other")
            unit = await _job(client, "unit", base, "mixed-other", True, original_branch="other")
            refusal = await failure(
                client,
                VERIFY,
                {
                    "branch": "feat/wt-proof",
                    "expected_source_head": base,
                    "expected_main_head": base,
                    "job_ids": [check, unit],
                },
            )
            assert "different controller" in refusal
            assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("wt") is None, reason="macOS Worktrunk CLI is required")
def test_review_plan_needs_main_ref_but_not_main_checkout(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base = await _fixture(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/controller")
        async with Client(create_server(_policy(tmp_path))) as client:
            created = await success(
                client,
                "create_project_worktrunk",
                {
                    "branch": "feat/wt-proof",
                    "expected_branch": "feat/controller",
                    "expected_head": base,
                    "expected_base_head": base,
                },
            )
            source_root = tmp_path / str(created["path"])
            (source_root / "feature.txt").write_text("review only\n")
            await git(source_root, "add", "feature.txt")
            await git(source_root, "commit", "-m", "feat: external review")
            source = await git(source_root, "rev-parse", "HEAD")
            jobs = [
                await _job(client, "check", base, "proposal-check", False, source, "feat/controller"),
                await _job(client, "unit", base, "proposal-unit", True, source, "feat/controller"),
            ]
            evidence = await success(
                client,
                VERIFY,
                {
                    "branch": "feat/wt-proof",
                    "expected_source_head": source,
                    "expected_main_head": base,
                    "job_ids": jobs,
                },
            )
            assert "branch refs/heads/main" not in await git(tmp_path, "worktree", "list", "--porcelain")
            plan = await success(
                client,
                "get_project_worktrunk_merge_plan",
                {
                    "evidence_id": evidence["evidence_id"],
                    "branch": "feat/wt-proof",
                    "expected_source_head": source,
                    "expected_main_head": base,
                },
            )
            assert plan["status"] == "approval_required"
            assert plan["target_head"] == base
            assert plan["source_head"] == source
            assert plan["write_requested"] is False
            assert await git(tmp_path, "rev-parse", "main") == base
            assert await git(tmp_path, "branch", "--show-current") == "feat/controller"

    asyncio.run(scenario())
