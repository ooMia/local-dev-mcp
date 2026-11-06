"""Bounded exact-head check observation; no merge policy enforcement or writes."""

from __future__ import annotations

import json
from typing import Literal, TypedDict, cast

from . import pr_merge

type QualityState = Literal["success", "pending", "failure", "missing", "unknown"]

_QUERY = """query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    url title headRefOid baseRefName state isDraft mergeCommit{oid} mergeable
    commits(last:1){nodes{commit{oid statusCheckRollup{state contexts(first:100){
      totalCount pageInfo{hasNextPage} nodes{__typename
        ... on CheckRun{name status conclusion detailsUrl checkSuite{app{slug}}}
        ... on StatusContext{context state targetUrl}
      }
    }}}}}
  }}
}"""


class CheckReport(TypedDict):
    name: str
    provider: str
    status: str
    conclusion: str | None
    url: str | None
    outcome: QualityState


class StatusReport(pr_merge.PlanReport):
    quality_state: QualityState
    mergeable: str
    rollup_state: str | None
    complete: bool
    reason: str | None
    checks: list[CheckReport]


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Invalid GitHub check object.")
    return cast(dict[str, object], value)


def _nodes(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Invalid GitHub check collection.")
    return cast(list[object], value)


def _check(value: object) -> CheckReport:
    data = _mapping(value)
    kind = data.get("__typename")
    provider = "commit-status"
    if kind == "CheckRun":
        name, status, conclusion, url = (data.get(key) for key in ("name", "status", "conclusion", "detailsUrl"))
        app = _mapping(data.get("checkSuite")).get("app")
        provider_value = _mapping(app).get("slug") if app is not None else ""
        if not isinstance(provider_value, str):
            raise ValueError("Invalid check provider.")
        provider = provider_value
        if status in ("QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"):
            outcome: QualityState = "pending"
        elif status == "COMPLETED" and conclusion == "SUCCESS":
            outcome = "success"
        elif status == "COMPLETED" and conclusion in (
            "ACTION_REQUIRED",
            "CANCELLED",
            "FAILURE",
            "NEUTRAL",
            "SKIPPED",
            "STALE",
            "STARTUP_FAILURE",
            "TIMED_OUT",
        ):
            outcome = "failure"
        else:
            outcome = "unknown"
    elif kind == "StatusContext":
        name, status, url = (data.get(key) for key in ("context", "state", "targetUrl"))
        conclusion = status
        outcome = (
            "success"
            if status == "SUCCESS"
            else "failure"
            if status in ("ERROR", "FAILURE")
            else "pending"
            if status in ("EXPECTED", "PENDING")
            else "unknown"
        )
    else:
        raise ValueError("Unknown check context type.")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(status, str)
        or (conclusion is not None and not isinstance(conclusion, str))
        or (url is not None and not isinstance(url, str))
    ):
        raise ValueError("Invalid check context fields.")
    return {
        "name": name,
        "provider": provider,
        "status": status,
        "conclusion": conclusion,
        "url": url,
        "outcome": outcome,
    }


def _checks(commit: dict[str, object], result: StatusReport) -> None:
    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        result.update(complete=True, reason=None, quality_state="missing")
        return
    data = _mapping(rollup)
    state = data.get("state")
    if state not in ("ERROR", "EXPECTED", "FAILURE", "PENDING", "SUCCESS"):
        raise ValueError("Unknown check rollup state.")
    result["rollup_state"] = cast(str, state)
    contexts = _mapping(data.get("contexts"))
    nodes = _nodes(contexts.get("nodes"))
    count = contexts.get("totalCount")
    complete = (
        type(count) is int
        and count == len(nodes)
        and len(nodes) <= 100
        and _mapping(contexts.get("pageInfo")).get("hasNextPage") is False
    )
    result["checks"] = [_check(node) for node in nodes]
    complete = complete and all(check["outcome"] != "unknown" for check in result["checks"])
    result["complete"] = complete
    if not complete:
        result["reason"] = "incomplete_check_contexts"
        return
    required = [
        check["outcome"]
        for check in result["checks"]
        if check["name"] == "Project quality" and check["provider"] == "github-actions"
    ]
    # Do not let one successful run conceal another matching failing/pending run.
    result["quality_state"] = (
        "missing"
        if not required
        else "failure"
        if "failure" in required
        else "pending"
        if "pending" in required
        else "success"
    )
    result["reason"] = None


async def inspect(run: pr_merge.GitHubRunner, repository: str, number: int, expected_head: str) -> StatusReport:
    observed = await pr_merge.observe(run, repository, number)
    if observed.head != expected_head:
        raise ValueError("PR head changed. Observe the current head before reading checks.")
    result: StatusReport = {
        **observed.report(),
        "quality_state": "unknown",
        "mergeable": "UNKNOWN",
        "rollup_state": None,
        "complete": False,
        "reason": "check_read_failed",
        "checks": [],
    }
    owner, name = repository.split("/")
    try:
        raw = await run(
            "api",
            "--hostname",
            "github.com",
            "graphql",
            "--input",
            "-",
            input_text=json.dumps({"query": _QUERY, "variables": {"owner": owner, "name": name, "number": number}}),
        )
        envelope = _mapping(json.loads(raw))
        if envelope.get("errors"):
            return result
        repo = _mapping(_mapping(envelope.get("data")).get("repository"))
        pr = _mapping(repo.get("pullRequest"))
    except pr_merge.MergeCommandError, ValueError:
        return result
    current = pr_merge.plan(repository, number, pr)
    if current.head != expected_head:
        raise ValueError("PR head changed while reading checks.")
    result = {**result, **current.report()}
    mergeable = pr.get("mergeable")
    if isinstance(mergeable, str) and mergeable in ("MERGEABLE", "CONFLICTING", "UNKNOWN"):
        result["mergeable"] = mergeable
    try:
        nodes = _nodes(_mapping(pr.get("commits")).get("nodes"))
        if len(nodes) != 1:
            raise ValueError("Cannot observe the PR head commit.")
        commit = _mapping(_mapping(nodes[0]).get("commit"))
    except ValueError:
        result["reason"] = "head_commit_unavailable"
        return result
    if commit.get("oid") != expected_head:
        raise ValueError("Check commit head differs from the reviewed PR head.")
    try:
        _checks(commit, result)
    except ValueError:
        result.update(complete=False, quality_state="unknown", reason="invalid_check_metadata")
    return result
