"""Deterministic squash requests and authoritative result observation."""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pytest

from local_dev_mcp.tools.gh import pr_merge

pytestmark = pytest.mark.unit
HEAD = "a" * 40
MERGED = "b" * 40
PARENT = "c" * 40
SUBJECT = "fix: 출처 보존 (#12)"
BODY = "Source-PR: https://github.com/owner/project/pull/12\nSource-Head: " + HEAD + "\n"
MESSAGE = SUBJECT + "\n\n" + BODY.rstrip("\n")


@dataclass
class GitHub:
    pr: dict[str, Any] = field(
        default_factory=lambda: {
            "url": "https://github.com/owner/project/pull/12",
            "title": "fix: 출처 보존",
            "headRefOid": HEAD,
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": False,
            "mergeCommit": None,
        }
    )
    commit: dict[str, Any] = field(
        default_factory=lambda: {"sha": MERGED, "message": MESSAGE, "parents": [{"sha": PARENT}]}
    )
    writes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    lose_response: bool = False
    stay_open: bool = False
    read_failure: bool = False

    async def __call__(self, *args: str, write: bool = False, input_text: str | None = None) -> str:
        if args[:2] == ("pr", "view"):
            assert args[args.index("--repo") + 1] == "https://github.com/owner/project"
            if self.read_failure and self.writes:
                raise pr_merge.MergeCommandError
            return json.dumps(self.pr)
        if write:
            assert args == (
                "api",
                "--hostname",
                "github.com",
                "repos/owner/project/pulls/12/merge",
                "--method",
                "PUT",
                "--input",
                "-",
            )
            assert input_text is not None
            self.writes.append(json.loads(input_text))
            if not self.stay_open:
                self.pr.update(state="MERGED", mergeCommit={"oid": MERGED})
            if self.lose_response:
                raise pr_merge.MergeCommandError
            return json.dumps({"merged": not self.stay_open, "sha": MERGED})
        assert args == ("api", "--hostname", "github.com", "repos/owner/project/git/commits/" + MERGED)
        return json.dumps(self.commit)


def execute(remote: GitHub) -> pr_merge.MergeResult:
    async def scenario() -> pr_merge.MergeResult:
        plan = await pr_merge.observe(remote, "owner/project", 12)
        return await pr_merge.merge(remote, "owner/project", 12, HEAD, plan.digest)

    return asyncio.run(scenario())


def test_plan_has_exact_message_and_digest_binds_reviewed_inputs() -> None:
    remote = GitHub()
    plan = asyncio.run(pr_merge.observe(remote, "owner/project", 12))
    assert plan.subject == SUBJECT
    assert plan.body == BODY
    assert len(plan.digest) == 64
    changed = deepcopy(remote.pr)
    changed["title"] = "fix: different intent"
    other = pr_merge.plan("owner/project", 12, changed)
    assert plan.digest != other.digest
    changed = deepcopy(remote.pr)
    changed.update(state="MERGED", mergeCommit={"oid": MERGED})
    assert plan.digest == pr_merge.plan("owner/project", 12, changed).digest


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("url", "https://github.com/other/project/pull/12"),
        ("headRefOid", "abc"),
        ("title", "fix: ok\nSource-PR: forged"),
        ("title", ""),
        ("title", "fix: bad\u2028line"),
        ("isDraft", 0),
        ("state", "UNKNOWN"),
        ("baseRefName", "develop"),
    ],
)
def test_invalid_remote_metadata_is_refused(key: str, value: object) -> None:
    data = GitHub().pr
    data[key] = value
    with pytest.raises(ValueError):
        pr_merge.plan("owner/project", 12, data)


def test_exact_payload_and_verified_actual_commit() -> None:
    remote = GitHub()
    result = execute(remote)
    assert remote.writes == [{"merge_method": "squash", "sha": HEAD, "commit_title": SUBJECT, "commit_message": BODY}]
    assert result["status"] == "merged_verified"
    assert result["merge_sha"] == MERGED
    assert result["actual_message"] == MESSAGE
    assert result["violations"] == []


@pytest.mark.parametrize("key,value", [("headRefOid", "d" * 40), ("title", "fix: changed title")])
def test_stale_review_does_not_write(key: str, value: str) -> None:
    remote = GitHub()
    reviewed = pr_merge.plan("owner/project", 12, remote.pr)
    remote.pr[key] = value
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(pr_merge.merge(remote, "owner/project", 12, HEAD, reviewed.digest))
    assert not remote.writes


@pytest.mark.parametrize("changes", [{"isDraft": True}, {"state": "CLOSED"}])
def test_unready_pr_is_not_written(changes: dict[str, object]) -> None:
    remote = GitHub()
    remote.pr.update(changes)
    with pytest.raises(ValueError, match="open, ready"):
        execute(remote)
    assert not remote.writes


def test_lost_write_response_observes_success_without_retry() -> None:
    remote = GitHub(lose_response=True)
    result = execute(remote)
    assert result["status"] == "merged_verified"
    assert result["request_uncertain"] is True
    assert len(remote.writes) == 1
    recovered = execute(remote)
    assert recovered["status"] == "merged_verified"
    assert recovered["write_requested"] is False
    assert len(remote.writes) == 1


@pytest.mark.parametrize("mode", ["open", "read_failure"])
def test_missing_confirmation_never_reports_success(mode: str) -> None:
    remote = GitHub(stay_open=mode == "open", read_failure=mode == "read_failure", lose_response=True)
    result = execute(remote)
    assert result["status"] == "unconfirmed"
    assert result["merge_sha"] is None
    assert len(remote.writes) == 1


@pytest.mark.parametrize(
    "changes,violation",
    [
        ({"message": "copied original commit"}, "message_mismatch"),
        ({"message": MESSAGE + "\n\nCo-authored-by: Person <person@example.com>"}, "message_mismatch"),
        ({"sha": "d" * 40}, "merge_sha_mismatch"),
        ({"parents": [{"sha": PARENT}, {"sha": HEAD}]}, "not_single_parent"),
    ],
)
def test_actual_result_mismatch_is_reported_with_merge_sha(changes: dict[str, object], violation: str) -> None:
    remote = GitHub()
    remote.commit.update(changes)
    result = execute(remote)
    assert result["status"] == "merged_mismatch"
    assert result["merge_sha"] == MERGED
    assert violation in result["violations"]


def test_cancellation_is_not_treated_as_a_merge_failure() -> None:
    async def cancelled(*args: str, write: bool = False, input_text: str | None = None) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pr_merge.merge(cancelled, "owner/project", 12, HEAD, "0" * 64))


def test_already_observed_merge_sha_survives_followup_failure() -> None:
    remote = GitHub()
    remote.pr.update(state="MERGED", mergeCommit={"oid": MERGED})
    reviewed = pr_merge.plan("owner/project", 12, remote.pr)
    reads = 0

    async def failing(*args: str, write: bool = False, input_text: str | None = None) -> str:
        nonlocal reads
        reads += 1
        if reads > 1:
            raise pr_merge.MergeCommandError
        return await remote(*args, write=write, input_text=input_text)

    result = asyncio.run(pr_merge.merge(failing, "owner/project", 12, HEAD, reviewed.digest))
    assert result["status"] == "unconfirmed"
    assert result["merge_sha"] == MERGED
    assert not result["write_requested"]


@pytest.mark.parametrize("parents", [[None], [{"sha": "short"}]])
def test_malformed_parent_metadata_cannot_verify_a_commit(parents: list[object]) -> None:
    remote = GitHub()
    remote.commit["parents"] = parents
    result = execute(remote)
    assert result["status"] == "merged_mismatch"
    assert "invalid_parent" in result["violations"]
