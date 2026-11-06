"""Shared, conditional direct-squash orchestration for CLI and MCP callers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, cast

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_FIELDS = "url,title,headRefOid,baseRefName,state,isDraft,mergeCommit"


class MergeCommandError(Exception):
    """A bounded adapter failed; its external diagnostics must not escape."""


class GitHubRunner(Protocol):
    async def __call__(self, *args: str, write: bool = False, input_text: str | None = None) -> str: ...


class PlanReport(TypedDict):
    repository: str
    number: int
    url: str
    head: str
    base: str
    state: str
    draft: bool
    merge_sha: str | None
    subject: str
    body: str
    plan_digest: str


class MergeResult(PlanReport):
    status: Literal["merged_verified", "merged_mismatch", "unconfirmed"]
    write_requested: bool
    request_uncertain: bool
    actual_message: str | None
    violations: list[str]


def _scope(repository: str, number: int) -> None:
    if (
        _REPOSITORY.fullmatch(repository) is None
        or any(part in (".", "..") for part in repository.split("/"))
        or type(number) is not int
        or number < 1
    ):
        raise ValueError("Select one GitHub repository and a positive PR number.")


def _sha(value: object) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError("GitHub did not return a full commit SHA.")
    return value


def _object(text: str) -> dict[str, object]:
    try:
        data: object = json.loads(text)
    except ValueError:
        raise ValueError("GitHub returned invalid JSON metadata.") from None
    if not isinstance(data, dict):
        raise ValueError("GitHub returned invalid object metadata.")
    return cast(dict[str, object], data)


@dataclass(frozen=True)
class MergePlan:
    repository: str
    number: int
    url: str
    head: str
    title: str
    state: str
    draft: bool
    merge_sha: str | None

    @property
    def subject(self) -> str:
        return f"{self.title} (#{self.number})"

    @property
    def body(self) -> str:
        return f"Source-PR: {self.url}\nSource-Head: {self.head}\n"

    @property
    def digest(self) -> str:
        intent = {
            "schema": 1,
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
            "base": "main",
            "head": self.head,
            "title": self.title,
            "subject": self.subject,
            "body": self.body,
        }
        return hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def report(self) -> PlanReport:
        return {
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
            "head": self.head,
            "base": "main",
            "state": self.state,
            "draft": self.draft,
            "merge_sha": self.merge_sha,
            "subject": self.subject,
            "body": self.body,
            "plan_digest": self.digest,
        }


def plan(repository: str, number: int, data: dict[str, object]) -> MergePlan:
    _scope(repository, number)
    url = data.get("url")
    title = data.get("title")
    state = data.get("state")
    draft = data.get("isDraft")
    if (
        not isinstance(url, str)
        or url.lower() != f"https://github.com/{repository}/pull/{number}".lower()
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 256
        or len(title.splitlines()) != 1
        or any(ord(char) < 32 or ord(char) == 127 for char in title)
        or not isinstance(state, str)
        or state not in ("OPEN", "CLOSED", "MERGED")
        or type(draft) is not bool
        or data.get("baseRefName") != "main"
    ):
        raise ValueError("GitHub PR identity, title, lifecycle or main base is invalid.")
    head = _sha(data.get("headRefOid"))
    commit = data.get("mergeCommit")
    merge_sha = None
    if state == "MERGED":
        if not isinstance(commit, dict):
            raise ValueError("Merged PR has no observable result commit.")
        merge_sha = _sha(cast(dict[str, object], commit).get("oid"))
    return MergePlan(repository, number, url, head, title, state, draft, merge_sha)


async def observe(run: GitHubRunner, repository: str, number: int) -> MergePlan:
    _scope(repository, number)
    metadata = await run("pr", "view", str(number), "--repo", f"https://github.com/{repository}", "--json", _FIELDS)
    return plan(repository, number, _object(metadata))


async def _result(
    run: GitHubRunner, intended: MergePlan, *, write_requested: bool, request_uncertain: bool
) -> MergeResult:
    result: MergeResult = {
        **intended.report(),
        "merge_sha": intended.merge_sha,
        "status": "unconfirmed",
        "write_requested": write_requested,
        "request_uncertain": request_uncertain,
        "actual_message": None,
        "violations": ["result_not_observed"],
    }
    try:
        current = await observe(run, intended.repository, intended.number)
        result["state"] = current.state
        result["draft"] = current.draft
        result["merge_sha"] = current.merge_sha
        if current.state != "MERGED":
            return result
        commit = _object(
            await run(
                "api",
                "--hostname",
                "github.com",
                f"repos/{intended.repository}/git/commits/{current.merge_sha}",
            )
        )
    except MergeCommandError, ValueError:
        return result
    violations: list[str] = []
    if current.digest != intended.digest:
        violations.append("pr_metadata_changed")
    if commit.get("sha") != current.merge_sha:
        violations.append("merge_sha_mismatch")
    parents = commit.get("parents")
    if not isinstance(parents, list) or len(cast(list[object], parents)) != 1:
        violations.append("not_single_parent")
    else:
        parent = cast(list[object], parents)[0]
        try:
            _sha(cast(dict[str, object], parent).get("sha") if isinstance(parent, dict) else None)
        except ValueError:
            violations.append("invalid_parent")
    message = commit.get("message")
    result["actual_message"] = message if isinstance(message, str) else None
    # Trailing LF is transport/storage normalization; all other text is compared exactly.
    if not isinstance(message, str) or message.rstrip("\n") != intended.subject + "\n\n" + intended.body.rstrip("\n"):
        violations.append("message_mismatch")
    result["violations"] = violations
    result["status"] = "merged_mismatch" if violations else "merged_verified"
    return result


async def merge(
    run: GitHubRunner, repository: str, number: int, expected_head: str, expected_plan_digest: str
) -> MergeResult:
    _sha(expected_head)
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest) is None:
        raise ValueError("A reviewed SHA-256 merge plan digest is required.")
    intended = await observe(run, repository, number)
    if intended.head != expected_head or intended.digest != expected_plan_digest:
        raise ValueError("PR head or merge plan changed. Review a fresh plan before merging.")
    if intended.state == "MERGED":
        return await _result(run, intended, write_requested=False, request_uncertain=False)
    if intended.state != "OPEN" or intended.draft:
        raise ValueError("Select an open, ready PR targeting main.")
    uncertain = False
    try:
        # Direct synchronous REST merge cannot silently switch to auto-merge or enqueue.
        await run(
            "api",
            "--hostname",
            "github.com",
            f"repos/{repository}/pulls/{number}/merge",
            "--method",
            "PUT",
            "--input",
            "-",
            write=True,
            input_text=json.dumps(
                {
                    "merge_method": "squash",
                    "sha": intended.head,
                    "commit_title": intended.subject,
                    "commit_message": intended.body,
                }
            ),
        )
    except MergeCommandError:
        uncertain = True
    return await _result(run, intended, write_requested=True, request_uncertain=uncertain)
