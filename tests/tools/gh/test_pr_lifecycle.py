"""Exact-head status and explicit Ready transitions without external resources."""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pytest

from local_dev_mcp.tools.gh import pr_merge, pr_ready, pr_status

pytestmark = pytest.mark.unit
HEAD = "a" * 40


def check(
    conclusion: str | None = "SUCCESS", status: str = "COMPLETED", provider: str = "github-actions"
) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": "Project quality",
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "https://github.com/owner/project/actions/runs/1/job/2",
        "checkSuite": {"app": {"slug": provider}},
    }


@dataclass
class GitHub:
    pr: dict[str, Any] = field(
        default_factory=lambda: {
            "url": "https://github.com/owner/project/pull/6",
            "title": "feat: lifecycle",
            "headRefOid": HEAD,
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": True,
            "mergeCommit": None,
        }
    )
    nodes: list[dict[str, Any]] = field(default_factory=lambda: [check()])
    next_page: bool = False
    errors: bool = False
    commit_head: str = HEAD
    graph_head: str = HEAD
    read_failure: bool = False
    graph_failure: bool = False
    fail_after_write: bool = False
    lose_response: bool = False
    remain_draft: bool = False
    change_after_write: bool = False
    writes: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    async def __call__(self, *args: str, write: bool = False, input_text: str | None = None) -> str:
        if write:
            assert args == ("pr", "ready", "6", "--repo", "https://github.com/owner/project")
            self.writes.append(args)
            self.pr["isDraft"] = self.remain_draft
            if self.change_after_write:
                self.pr["headRefOid"] = "d" * 40
            if self.lose_response:
                raise pr_merge.MergeCommandError
            return ""
        if self.read_failure or (self.fail_after_write and self.writes):
            raise pr_merge.MergeCommandError
        if args[:2] == ("pr", "view"):
            return json.dumps(self.pr)
        assert args == ("api", "--hostname", "github.com", "graphql", "--input", "-")
        if self.graph_failure:
            raise pr_merge.MergeCommandError
        assert input_text is not None
        payload = json.loads(input_text)
        assert payload["variables"] == {"owner": "owner", "name": "project", "number": 6}
        pr = deepcopy(self.pr)
        pr.update(
            headRefOid=self.graph_head,
            mergeable="MERGEABLE",
            commits={
                "nodes": [
                    {
                        "commit": {
                            "oid": self.commit_head,
                            "statusCheckRollup": {
                                "state": "SUCCESS",
                                "contexts": {
                                    "nodes": self.nodes,
                                    "totalCount": len(self.nodes),
                                    "pageInfo": {"hasNextPage": self.next_page},
                                },
                            },
                        }
                    }
                ]
            },
        )
        result: dict[str, Any] = {"data": {"repository": {"pullRequest": pr}}}
        if self.errors:
            result["errors"] = [{"message": "partial response"}]
        return json.dumps(result)


@pytest.mark.parametrize(
    "conclusion,status,expected",
    [
        ("SUCCESS", "COMPLETED", "success"),
        ("FAILURE", "COMPLETED", "failure"),
        ("CANCELLED", "COMPLETED", "failure"),
        ("SKIPPED", "COMPLETED", "failure"),
        (None, "IN_PROGRESS", "pending"),
        (None, "QUEUED", "pending"),
        (None, "COMPLETED", "unknown"),
    ],
)
def test_quality_classifies_only_actual_success(conclusion: str | None, status: str, expected: str) -> None:
    remote = GitHub(nodes=[check(conclusion, status)])
    result = asyncio.run(pr_status.inspect(remote, "owner/project", 6, HEAD))
    assert result["quality_state"] == expected
    assert result["head"] == HEAD
    assert not remote.writes


@pytest.mark.parametrize(
    "nodes",
    [
        [],
        [check(provider="another-app")],
        [
            {
                "__typename": "StatusContext",
                "context": "Project quality",
                "state": "SUCCESS",
                "targetUrl": None,
            }
        ],
    ],
)
def test_absent_or_spoofed_quality_is_not_success(nodes: list[dict[str, Any]]) -> None:
    result = asyncio.run(pr_status.inspect(GitHub(nodes=nodes), "owner/project", 6, HEAD))
    assert result["quality_state"] == "missing"


@pytest.mark.parametrize(
    "remote", [GitHub(next_page=True), GitHub(errors=True), GitHub(nodes=[{"__typename": "FutureType"}])]
)
def test_partial_or_unknown_contexts_cannot_report_success(remote: GitHub) -> None:
    result = asyncio.run(pr_status.inspect(remote, "owner/project", 6, HEAD))
    assert result["quality_state"] == "unknown"
    assert not result["complete"]


@pytest.mark.parametrize("field", ["graph_head", "commit_head"])
def test_checks_for_another_head_are_refused(field: str) -> None:
    remote = GitHub()
    setattr(remote, field, "d" * 40)
    with pytest.raises(ValueError, match="head"):
        asyncio.run(pr_status.inspect(remote, "owner/project", 6, HEAD))


def test_success_does_not_hide_another_failed_or_running_quality_run() -> None:
    for other, expected in ((check("FAILURE"), "failure"), (check(None, "IN_PROGRESS"), "pending")):
        result = asyncio.run(pr_status.inspect(GitHub(nodes=[other, check()]), "owner/project", 6, HEAD))
        assert result["quality_state"] == expected


def ready(remote: GitHub, digest: str | None = None) -> pr_ready.ReadyResult:
    reviewed = pr_merge.plan("owner/project", 6, remote.pr)
    return asyncio.run(pr_ready.mark_ready(remote, "owner/project", 6, HEAD, digest or reviewed.digest))


def test_ready_pins_reviewed_plan_and_observes_lost_response_without_replay() -> None:
    remote = GitHub(lose_response=True)
    result = ready(remote)
    assert result["status"] == "ready_verified"
    assert result["request_uncertain"] is True
    assert len(remote.writes) == 1
    result = ready(remote)
    assert result["status"] == "ready_verified"
    assert not result["write_requested"]
    assert len(remote.writes) == 1


def test_stale_ready_plan_is_refused_before_write() -> None:
    remote = GitHub()
    with pytest.raises(ValueError, match="changed"):
        ready(remote, "0" * 64)
    assert not remote.writes


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_ready_refuses_nonopen_pr(state: str) -> None:
    remote = GitHub()
    remote.pr.update(state=state, mergeCommit={"oid": "b" * 40})
    with pytest.raises(ValueError, match="open"):
        ready(remote)
    assert not remote.writes


def test_ready_still_draft_is_unconfirmed() -> None:
    result = ready(GitHub(remain_draft=True))
    assert result["status"] == "unconfirmed"


def test_ready_detects_head_change_after_nonatomic_write() -> None:
    result = ready(GitHub(change_after_write=True))
    assert result["status"] == "ready_mismatch"
    assert "pr_metadata_changed" in result["violations"]


def test_check_transport_failure_remains_unknown() -> None:
    result = asyncio.run(pr_status.inspect(GitHub(graph_failure=True), "owner/project", 6, HEAD))
    assert result["quality_state"] == "unknown" and not result["complete"]
    assert result["head"] == HEAD


def test_ready_followup_failure_does_not_claim_completion() -> None:
    remote = GitHub(fail_after_write=True)
    result = ready(remote)
    assert result["status"] == "unconfirmed"
    assert result["write_requested"] and len(remote.writes) == 1


def test_ready_cancellation_propagates() -> None:
    async def cancelled(*args: str, write: bool = False, input_text: str | None = None) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pr_ready.mark_ready(cancelled, "owner/project", 6, HEAD, "0" * 64))
