"""Explicit Ready transition with reviewed intent and authoritative readback."""

import re
from typing import Literal

from . import pr_merge


class ReadyResult(pr_merge.PlanReport):
    status: Literal["ready_verified", "ready_mismatch", "unconfirmed"]
    write_requested: bool
    request_uncertain: bool
    violations: list[str]


async def mark_ready(
    run: pr_merge.GitHubRunner, repository: str, number: int, expected_head: str, expected_plan_digest: str
) -> ReadyResult:
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest) is None:
        raise ValueError("A reviewed merge plan digest is required.")
    intended = await pr_merge.observe(run, repository, number)
    if intended.head != expected_head or intended.digest != expected_plan_digest:
        raise ValueError("PR head or plan changed. Review a fresh plan.")
    if intended.state != "OPEN":
        raise ValueError("Select an open main PR for Ready transition.")
    result: ReadyResult = {
        **intended.report(),
        "status": "unconfirmed",
        "write_requested": intended.draft,
        "request_uncertain": False,
        "violations": ["ready_not_observed"],
    }
    if intended.draft:
        try:
            # GitHub Ready has no atomic expected-head condition; verify again below.
            await run("pr", "ready", str(number), "--repo", f"https://github.com/{repository}", write=True)
        except pr_merge.MergeCommandError:
            result["request_uncertain"] = True
    try:
        current = await pr_merge.observe(run, repository, number)
    except pr_merge.MergeCommandError, ValueError:
        return result
    result = {**result, **current.report()}
    if current.digest != intended.digest or current.state != "OPEN":
        result.update(status="ready_mismatch", violations=["pr_metadata_changed"])
    elif not current.draft:
        result.update(status="ready_verified", violations=[])
    return result
