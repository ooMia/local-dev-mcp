"""Read-only WSL tool catalog and host preflight for the current workspace.

Catalog presence and startup readiness are not execution success. Only explicit
read probes below run through a real stdio MCP child; mutating tools are never
called by this command. The full acceptance suite remains the pytest tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from local_dev_mcp.mcp.server import TOOLS

# Keep this inventory deliberate: introducing/removing a public tool requires
# assigning it an audit family. No permission/functional success is inferred.
TOOL_FAMILIES: dict[str, tuple[str, ...]] = {
    "local_git": ("get_project_head", "get_main_head", "get_project_status", "get_project_diff", "get_project_ref", "inspect_project_commit_history", "inspect_project_policy", "compare_project_refs", "consolidate_project_history", "stage_project_paths", "unstage_project_paths", "commit_project_changes", "create_project_branch", "merge_project_branch", "create_project_branch_from_ref", "switch_project_branch", "get_project_pick_state", "cherry_pick_project_commit", "get_project_worktree_pick_state", "cherry_pick_project_worktree_commit", "commit_project_worktree_pick", "restore_project_pick_paths",),
    "remote_git": ("diagnose_project_remote_auth", "inspect_project_remote", "fetch_project_remote_main", "push_project_feature", "create_project_draft_pr", "get_project_pr_merge_plan", "get_project_pr_status", "mark_project_pr_ready", "merge_project_pr", "inspect_project_branch_remote", "fetch_project_branch_remote_main", "push_project_branch", "create_project_branch_draft_pr",),
    "native_worktree": ("prepare_project_worktree", "finalize_project_worktree", "inspect_project_worktree", "prepare_project_worktree_assessment", "commit_project_worktree_assessment", "start_project_worktree_task",),
    "worktrunk": ("create_project_worktrunk", "inspect_project_worktrunk", "list_project_worktrees", "get_project_worktree_review", "plan_project_worktree_cleanup", "verify_project_worktrunk_quality", "get_project_worktrunk_merge_plan", "remove_project_worktrunk",),
    "release": ("get_release_plan", "prepare_project_release",),
    "readiness": ("get_tool_readiness",),
    "trail": ("get_project_index_status", "refresh_project_index", "get_project_worktree_index_status", "refresh_project_worktree_index",),
    "elicitation": ("diagnose_client_elicitation", "probe_client_elicitation", "probe_client_elicitation_mrtr",),
    "tasks": ("list_project_tasks", "start_project_task", "get_project_task", "cancel_project_task",),
    "workspace": ("create_workspace_file", "delete_workspace_file", "replace_workspace_file", "list_workspace_entries", "find_workspace_paths", "search_workspace_text", "read_workspace_file",),
    "documents": ("diagnose_documents",),
}

BINARIES = ("uv", "git", "gh", "gpg", "gpgconf", "wt", "trail", "tunnel-client")
READ_PROBES: dict[str, dict[str, object]] = {
    "get_tool_readiness": {},
    "get_project_head": {},
    "get_main_head": {},
    "get_project_status": {},
    "list_workspace_entries": {"path": ".", "limit": 1},
    "find_workspace_paths": {"path": ".", "pattern": "**/*.py", "limit": 1},
    "list_project_tasks": {},
    "list_project_worktrees": {"include_branches": False},
    "get_project_index_status": {},
    "diagnose_client_elicitation": {},
    "get_release_plan": {},
}


def catalog_names() -> list[str]:
    return [definition.name for definition in TOOLS]


def inventory_issues() -> dict[str, list[str]]:
    expected = catalog_names()
    grouped = [name for members in TOOL_FAMILIES.values() for name in members]
    return {
        "missing": sorted(set(expected) - set(grouped)),
        "unexpected": sorted(set(grouped) - set(expected)),
        "duplicate": sorted({name for name in grouped if grouped.count(name) != 1}),
    }


def is_wsl(*, kernel: str | None = None, distro: str | None = None) -> bool:
    observed_kernel = platform.release() if kernel is None else kernel
    observed_distro = os.environ.get("WSL_DISTRO_NAME", "") if distro is None else distro
    return sys.platform == "linux" and (bool(observed_distro) or "microsoft" in observed_kernel.casefold())


def binary_inventory() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name in BINARIES:
        found = shutil.which(name)
        # Report resolved command path, not any environment or credential values.
        location = str(Path(found).resolve()) if found else None
        result[name] = {
            "available": found is not None,
            "path": location,
            "windows_interop_candidate": bool(location and location.casefold().endswith(".exe")),
        }
    return result


async def inspect_stdio(workspace: Path, report: dict[str, Any]) -> None:
    # The child loads exactly this workspace's config and .env; no credentials
    # or returned file contents are emitted in the report.
    server = StdioServerParameters(
        command=sys.executable,
        args=["-I", "-m", "local_dev_mcp", "--workspace", str(workspace), "run"],
        cwd=workspace,
    )
    async with Client(server, read_timeout_seconds=30, cache=None) as session:
        advertised = [tool.name for tool in (await session.list_tools()).tools]
        expected = catalog_names()
        report["catalog"] = {
            "state": "PASS" if advertised == expected else "FAIL",
            "advertised": len(advertised),
            "expected": len(expected),
            "missing": sorted(set(expected) - set(advertised)),
            "unexpected": sorted(set(advertised) - set(expected)),
            "ordered": advertised == expected,
        }
        readiness = await session.call_tool("get_tool_readiness", raise_on_error=False)
        details = readiness.structured_content if not readiness.is_error else None
        if not isinstance(details, dict) or not isinstance(details.get("tools"), list):
            report["readiness"] = {"state": "FAIL", "reason": "unavailable_or_invalid"}
            return
        states = {
            value["name"]: value["ready"]
            for value in details["tools"]
            if isinstance(value, dict) and isinstance(value.get("name"), str) and isinstance(value.get("ready"), bool)
        }
        report["readiness"] = {
            "state": "PASS" if set(states) == set(expected) else "FAIL",
            "ready": sum(states.values()),
            "total": len(states),
            "not_ready": sorted(name for name, ready in states.items() if not ready),
        }
        for name, arguments in READ_PROBES.items():
            if name not in advertised:
                report["read_probes"][name] = "NOT_RUN"
                continue
            if states.get(name) is False:
                report["read_probes"][name] = "BLOCKED"
                continue
            try:
                response = await session.call_tool(name, arguments, raise_on_error=False)
                report["read_probes"][name] = (
                    "PASS" if not response.is_error and isinstance(response.structured_content, dict) else "FAIL"
                )
            except Exception:
                report["read_probes"][name] = "FAIL"


def build_report(workspace: Path) -> dict[str, Any]:
    names = catalog_names()
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "host": {"system": platform.system(), "kernel": platform.release(), "wsl": is_wsl()},
        "binaries": binary_inventory(),
        "inventory_issues": inventory_issues(),
        "catalog": {"state": "NOT_RUN", "expected": len(names)},
        "readiness": {"state": "NOT_RUN"},
        "read_probes": {name: "NOT_RUN" for name in READ_PROBES},
        "tools": [
            {"name": name, "family": family, "functional": "NOT_RUN"}
            for family, members in TOOL_FAMILIES.items()
            for name in members
        ],
        "evidence_boundary": "catalog/readiness/read-only smoke only; mutating and external tools are NOT_RUN",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only LOCAL-DEV-MCP WSL tool preflight")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--require-wsl", action="store_true")
    parser.add_argument("--catalog-only", action="store_true", help="Do not start an MCP child")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    report = build_report(workspace)
    try:
        if not args.catalog_only:
            asyncio.run(inspect_stdio(workspace, report))
    except Exception as error:
        report["connection"] = {"state": "FAIL", "error_type": type(error).__name__}
    issues = report["inventory_issues"]
    invalid_inventory = any(issues.values())
    invalid_host = args.require_wsl and not report["host"]["wsl"]
    failed = (
        invalid_inventory
        or invalid_host
        or report["catalog"]["state"] == "FAIL"
        or report["readiness"]["state"] == "FAIL"
        or report.get("connection", {}).get("state") == "FAIL"
        or "FAIL" in report["read_probes"].values()
    )
    if invalid_host:
        report["host"]["gate"] = "BLOCKED_NOT_WSL"
    report["outcome"] = "FAIL" if failed else "PASS_PRECHECK_ONLY"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
