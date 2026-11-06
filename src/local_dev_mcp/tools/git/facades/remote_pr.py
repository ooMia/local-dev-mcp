"""Constrained MCP publication of a project feature through its existing private GitHub origin."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, TypedDict, cast

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....errors import DependencyError
from ....mcp.registry import ToolDefinition
from ....readiness import PreparationIssue
from ... import executable, workflow_pr
from .. import _remote
from .._prepare import git_readiness
from .._remote import Remote, feature_name, title, valid_sha
from ._refs import ExpectedHead
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

type Purpose = Annotated[
    str | None, Field(max_length=500, description="Reviewed purpose if assessments have no unique reason.")
]

type RemoteBranchHead = Annotated[
    str | None, Field(description="Previously observed remote feature head; null means the branch must not exist.")
]


class RemoteState(TypedDict):
    project: str
    repository: str
    branch: str
    head: str
    local_main: str | None
    tracking_main: str | None
    remote_main: str
    remote_feature: str | None
    tracking_is_fresh: bool
    feature_contains_remote: bool | None
    unpublished_main_in_feature: bool | None
    observed_at: str


class RemoteAuthReport(TypedDict):
    project: str
    home_source: str
    home_inside_workspace: bool
    gh_config_dir: str
    gh_auth_exit_code: int | None
    repo_access: str
    git_transport: str
    action: str
    observed_at: str


class FetchedMain(TypedDict):
    project: str
    branch: str
    head: str
    tracking_main_before: str | None
    tracking_main_after: str
    remote_main: str
    changed: bool
    observed_at: str


class PushedFeature(TypedDict):
    project: str
    branch: str
    head: str
    remote_before: str | None
    remote_after: str
    changed: bool
    observed_at: str


class PullRequestResult(TypedDict):
    project: str
    branch: str
    head: str
    base: str
    number: int
    url: str
    draft: bool
    created: bool
    observed_at: str


def remote_readiness(settings: ServerSettings) -> PreparationIssue | None:
    issue = git_readiness(settings)
    if issue is not None:
        return issue
    try:
        executable("gh")
    except DependencyError as error:
        return PreparationIssue.from_error(error)
    return None


def require_permission(settings: ServerSettings, capability: str) -> None:
    try:
        for permission in ("git", "gh", capability):
            settings.policy.require_command(permission)
        settings.policy.require_project_access()
    except PermissionError as error:
        raise ToolError(str(error)) from None


async def _checked(remote: Remote, expected_branch: str, expected_head: str) -> tuple[str, str]:
    branch, head, _ = await remote.snapshot()
    if branch != feature_name(expected_branch) or head != valid_sha(expected_head):
        raise ToolError("Local feature branch or HEAD changed. Re-observe project status.")
    return branch, head


async def _baseline(remote: Remote) -> tuple[str, str, str]:
    repository, url = await remote.repo()
    remote_main = await remote.main_sha(repository)
    tracking = await remote.local_sha("refs/remotes/origin/main")
    if tracking != remote_main:
        raise ToolError("origin/main is stale. Fetch the remote main using the guarded MCP tool.")
    return url, repository, remote_main


async def _unpublished_local_main_in_feature(remote: Remote, base: str, head: str) -> bool:
    local_main = await remote.local_sha("refs/heads/main")
    return (
        local_main is not None
        and local_main != base
        and await remote.ancestor(local_main, head)
        and not await remote.ancestor(local_main, base)
    )


async def _require_clean_public_history(remote: Remote, base: str, head: str) -> None:
    if await _unpublished_local_main_in_feature(remote, base, head):
        raise ToolError(
            "Feature contains local-main commits absent from remote main. "
            "Resolve the unpublished main history before publishing one logical PR."
        )


@dataclass(frozen=True)
class DiagnoseRemoteAuthTool:
    settings: ServerSettings

    async def execute(self) -> RemoteAuthReport:
        """Report only authentication outcomes and configuration classes, not secrets."""
        require_permission(self.settings, "git.remote.read")
        remote = Remote(self.settings)
        effective = _remote.env()
        home = effective.get("HOME")
        inherited = os.environ.get("HOME")
        home_source = (
            "inherited" if inherited and Path(inherited).is_absolute() else "os_account" if home else "missing"
        )
        config = os.environ.get("GH_CONFIG_DIR")
        config_class = (
            "default" if not config else "explicit_absolute" if Path(config).is_absolute() else "relative_ignored"
        )

        def inside_project(value: str | None) -> bool:
            return bool(
                value and Path(value).is_absolute() and Path(value).resolve().is_relative_to(remote.root.resolve())
            )

        home_inside = inside_project(home)
        if (
            home is None
            or home_inside
            or any(inside_project(effective.get(key)) for key in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME"))
        ):
            return {
                "project": str(remote.root),
                "home_source": home_source,
                "home_inside_workspace": home_inside,
                "gh_config_dir": config_class,
                "gh_auth_exit_code": None,
                "repo_access": "not_checked",
                "git_transport": "not_checked",
                "action": "Fix the account configuration location; no gh command was run.",
                "observed_at": observed_at(),
            }
        auth = await remote.run("gh", "auth", "status", "--hostname", "github.com", allow_failure=True)
        repo_access = "not_checked"
        git_transport = "not_checked"
        action = "Authenticate the existing host GitHub CLI account, without passing tokens through MCP."
        if auth.code == 0:
            try:
                repository, _ = await remote.repo()
            except ToolError, ValueError, KeyError, TypeError:
                repo_access = "unavailable_or_mismatched"
                action = "GitHub login exists; verify origin repository access and gh repository context."
            else:
                repo_access = "matched"
                try:
                    api_head = await remote.main_sha(repository)
                    git_head = await remote.branch("main")
                except ToolError, ValueError, KeyError, TypeError:
                    git_transport = "unavailable"
                    action = (
                        "GitHub API authentication works, but Git remote transport failed. "
                        "Check the scoped gh credential helper."
                    )
                else:
                    git_transport = "matched" if api_head == git_head else "mismatched"
                    action = (
                        "GitHub API and Git transport agree; remote ref inspection can proceed."
                        if git_transport == "matched"
                        else "GitHub API and Git remote refs differ; retry observation before publication."
                    )
        return {
            "project": str(remote.root),
            "home_source": home_source,
            "home_inside_workspace": home_inside,
            "gh_config_dir": config_class,
            "gh_auth_exit_code": auth.code,
            "repo_access": repo_access,
            "git_transport": git_transport,
            "action": action,
            "observed_at": observed_at(),
        }


@dataclass(frozen=True)
class InspectRemoteTool:
    settings: ServerSettings

    async def execute(self) -> RemoteState:
        require_permission(self.settings, "git.remote.read")
        remote = Remote(self.settings)
        branch, head, _ = await remote.snapshot()
        repo, _ = await remote.repo()
        main = await remote.main_sha(repo)
        tracking = await remote.local_sha("refs/remotes/origin/main")
        local_main = await remote.local_sha("refs/heads/main")
        available = await remote.local_sha(main) is not None
        return {
            "project": str(remote.root),
            "repository": repo,
            "branch": branch,
            "head": head,
            "local_main": local_main,
            "tracking_main": tracking,
            "remote_main": main,
            "remote_feature": await remote.branch(branch) if branch != "main" else None,
            "tracking_is_fresh": tracking == main,
            "feature_contains_remote": await remote.ancestor(main, head) if available else None,
            "unpublished_main_in_feature": await _unpublished_local_main_in_feature(remote, main, head)
            if available
            else None,
            "observed_at": observed_at(),
        }


@dataclass(frozen=True)
class FetchRemoteMainTool:
    settings: ServerSettings

    async def execute(
        self, expected_branch: str, expected_head: ExpectedHead, expected_remote_main: ExpectedHead
    ) -> FetchedMain:
        require_permission(self.settings, "git.remote.write")
        remote = Remote(self.settings)
        branch, head = await _checked(remote, expected_branch, expected_head)
        repository, _ = await remote.repo()
        actual = await remote.main_sha(repository)
        if actual != expected_remote_main:
            raise ToolError("Remote main changed since observation. Inspect again before fetch.")
        direct = await remote.git("symbolic-ref", "--quiet", "refs/remotes/origin/main", allow_failure=True)
        if direct.code == 0:
            raise ToolError("The tracking destination is symbolic; indirect ref updates are not permitted.")
        if direct.code != 1:
            raise ToolError("Cannot verify that the tracking destination is a direct ref.")
        before = await remote.local_sha("refs/remotes/origin/main")
        # Download the observed main object without touching refs or FETCH_HEAD first.
        # Ancestry cannot be checked against a newly merged SHA before it exists locally.
        if before != actual and await remote.local_sha(actual) is None:
            await remote.git(
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--refmap=",
                "origin",
                actual,
                write=True,
            )
        if before is not None and before != actual and not await remote.ancestor(before, actual):
            raise ToolError("Remote main was rewritten or diverged; automatic tracking update is not permitted.")
        if before != actual:
            if await remote.main_sha(repository) != actual:
                raise ToolError("Remote main changed during fetch; tracking ref was not updated. Inspect again.")
            await _checked(remote, branch, head)
            await remote.git(
                "update-ref",
                "--no-deref",
                "refs/remotes/origin/main",
                actual,
                before or "0" * len(actual),
                write=True,
            )
        after = await remote.local_sha("refs/remotes/origin/main")
        if after != actual or (await remote.local_sha("HEAD")) != head:
            raise ToolError("Fetch result did not match observed main/HEAD; inspect local and remote refs.")
        return {
            "project": str(remote.root),
            "branch": branch,
            "head": head,
            "tracking_main_before": before,
            "tracking_main_after": valid_sha(after),
            "remote_main": actual,
            "changed": before != after,
            "observed_at": observed_at(),
        }


@dataclass(frozen=True)
class PushFeatureTool:
    settings: ServerSettings

    async def execute(
        self,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_remote_main: ExpectedHead,
        expected_remote_feature: RemoteBranchHead = None,
    ) -> PushedFeature:
        require_permission(self.settings, "git.remote.write")
        remote = Remote(self.settings)
        branch, head = await _checked(remote, expected_branch, expected_head)
        _, _, base = await _baseline(remote)
        if base != expected_remote_main:
            raise ToolError("Remote main changed. Inspect the new baseline before pushing.")
        if not await remote.ancestor(base, head):
            raise ToolError("The feature branch does not contain current remote main. Reconcile before publishing.")
        await _require_clean_public_history(remote, base, head)
        before = await remote.branch(branch)
        if before != expected_remote_feature:
            raise ToolError("Remote feature ref changed since observation; no push performed.")
        await _checked(remote, branch, head)
        if before != head:
            # No --force, no alternate refspec, no hook bypass, and no tags.
            await remote.git(
                "push", "--porcelain", "--no-follow-tags", "origin", head + ":refs/heads/" + branch, write=True
            )
        after = await remote.branch(branch)
        if after != head or (await remote.local_sha("HEAD")) != head:
            raise ToolError("Push may have completed but its target was not confirmed. Inspect before retrying.")
        return {
            "project": str(remote.root),
            "branch": branch,
            "head": head,
            "remote_before": before,
            "remote_after": valid_sha(after),
            "changed": before != after,
            "observed_at": observed_at(),
        }


async def _facts(remote: Remote, base: str, head: str, url: str) -> dict[str, Any]:
    log = (await remote.git("log", "--reverse", "-z", "--format=%H%x00%s", base + ".." + head)).output
    parts = log.rstrip("\x00").split("\x00") if log else []
    if not parts or len(parts) % 2:
        raise ToolError("Incomplete feature history; cannot produce a PR body.")
    commits: list[dict[str, str]] = []
    for sha, subject in zip(parts[::2], parts[1::2], strict=True):
        commits.append({"sha": valid_sha(sha), "title": subject.strip()})
    changed = (await remote.git("diff", "--name-only", "--diff-filter=A", "-z", base, head, "--", ".changes/")).output
    assessments: list[dict[str, str]] = []
    for path in sorted(filter(None, changed.split("\x00"))):
        if re.fullmatch(r"\.changes/[a-z0-9][a-z0-9-]*\.toml", path) is None:
            raise ToolError("Unexpected assessment path in PR.")
        record = tomllib.loads((await remote.git("show", head + ":" + path)).output)
        record.setdefault("migration", "")
        fields = ("impact", "summary", "reason", "migration")
        if record.get("impact") not in ("none", "patch", "minor", "major") or any(
            not isinstance(record.get(field), str) for field in fields
        ):
            raise ToolError("Invalid change assessment for PR generation.")
        if not record["reason"].strip() or not record["summary"].strip():
            raise ToolError("Incomplete change assessment.")
        assessments.append({"id": path[9:-5], **{key: cast(str, record[key]) for key in fields}})
    if not assessments:
        raise ToolError("No new explicit .changes assessments in this PR.")
    return {
        "schema": 1,
        "repo_url": url,
        "base": base,
        "merge_base": base,
        "head": head,
        "commits": commits,
        "assessments": assessments,
    }


async def _existing(remote: Remote, branch: str, repository_url: str) -> list[dict[str, Any]]:
    response = json.loads(
        (
            await remote.gh(
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number,url,title,headRefOid,baseRefName,isDraft,state",
            )
        ).output
    )
    if not isinstance(response, list):
        raise ToolError("PR discovery did not return a list.")
    candidates = cast(list[object], response)
    if len(candidates) >= 100:
        raise ToolError("PR discovery was incomplete; inspect GitHub before creating a PR.")
    if any(not isinstance(row, dict) for row in candidates):
        raise ToolError("Unexpected PR identity in GitHub results.")
    rows = cast(list[dict[str, Any]], candidates)
    if any(not isinstance(row.get("url"), str) or not row["url"].startswith(repository_url + "/pull/") for row in rows):
        raise ToolError("Unexpected PR identity in GitHub results.")
    return rows


@dataclass(frozen=True)
class CreateFeaturePRTool:
    settings: ServerSettings
    template_root: Path | None = None

    async def execute(
        self,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_remote_main: ExpectedHead,
        title_text: str,
        purpose: Purpose = None,
        exceptions: list[dict[str, str]] | None = None,
    ) -> PullRequestResult:
        require_permission(self.settings, "gh.pr.create")
        remote = Remote(self.settings)
        branch, head = await _checked(remote, expected_branch, expected_head)
        url, _, base = await _baseline(remote)
        if base != expected_remote_main or not await remote.ancestor(base, head):
            raise ToolError("PR baseline moved or feature does not contain remote main.")
        await _require_clean_public_history(remote, base, head)
        if await remote.branch(branch) != head:
            raise ToolError("Remote feature has not been pushed at the observed exact SHA.")
        title(title_text)
        rows = await _existing(remote, branch, url)
        if rows:
            current = rows[0]
            if (
                len(rows) != 1
                or current["state"] != "OPEN"
                or current["headRefOid"] != head
                or current["baseRefName"] != "main"
            ):
                raise ToolError("The feature already has a different or closed PR; no new PR was created.")
            return {
                "project": str(remote.root),
                "branch": branch,
                "head": head,
                "base": base,
                "number": current["number"],
                "url": current["url"],
                "draft": current["isDraft"],
                "created": False,
                "observed_at": observed_at(),
            }
        facts = await _facts(remote, base, head, url)
        context: dict[str, Any] = {}
        if purpose is not None:
            context["purpose"] = purpose
        if exceptions:
            context["exceptions"] = exceptions
        try:
            body = workflow_pr.render(self.template_root or remote.root, facts, context)
            workflow_pr.parse(body)
        except OSError, ValueError, KeyError, TypeError:
            raise ToolError("PR body could not be safely generated; review assessments and purpose.") from None
        if len(body.encode("utf-8")) > 48000:
            raise ToolError("PR body exceeds the scoped publication limit.")
        if await remote.main_sha((await remote.repo())[0]) != base or await remote.branch(branch) != head:
            raise ToolError("Remote refs changed before PR creation.")
        await _checked(remote, branch, head)
        made = (
            await remote.gh(
                "pr",
                "create",
                "--draft",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                title_text,
                "--body-file",
                "-",
                write=True,
                input_text=body,
            )
        ).output.strip()
        match = re.fullmatch(re.escape(url) + r"/pull/([1-9][0-9]*)", made)
        if match is None:
            raise ToolError(
                "PR may have been created but its URL was not confirmed; inspect existing PRs before retrying."
            )
        number = int(match[1])
        latest = await _existing(remote, branch, url)
        selected = [row for row in latest if row.get("number") == number]
        if (
            len(selected) != 1
            or selected[0]["headRefOid"] != head
            or not selected[0]["isDraft"]
            or selected[0]["baseRefName"] != "main"
            or selected[0]["state"] != "OPEN"
        ):
            raise ToolError("PR creation result could not be confirmed; inspect GitHub before retrying.")
        confirmed = json.loads(
            (await remote.gh("pr", "view", str(number), "--json", "body,headRefOid,baseRefOid")).output
        )
        if confirmed.get("body") != body or confirmed.get("headRefOid") != head or confirmed.get("baseRefOid") != base:
            raise ToolError("PR creation base/head/body could not be confirmed; inspect GitHub before retrying.")
        if len(facts["commits"]) > 5:
            linked = workflow_pr.render(self.template_root or remote.root, facts, context, number=number)
            await remote.gh("pr", "edit", str(number), "--body-file", "-", write=True, input_text=linked)
            inspected = json.loads(
                (await remote.gh("pr", "view", str(number), "--json", "body,headRefOid,baseRefOid")).output
            )
            if (
                inspected.get("body") != linked
                or inspected.get("headRefOid") != head
                or inspected.get("baseRefOid") != base
            ):
                raise ToolError(
                    "PR was created but its commit-history link was not confirmed; inspect before retrying."
                )
        return {
            "project": str(remote.root),
            "branch": branch,
            "head": head,
            "base": base,
            "number": number,
            "url": made,
            "draft": True,
            "created": True,
            "observed_at": observed_at(),
        }


DIAGNOSE_PROJECT_REMOTE_AUTH: Final = ToolDefinition(
    name="diagnose_project_remote_auth",
    description=(
        "Check GitHub CLI login and private origin access without exposing tokens, user IDs, "
        "credential paths or command output. Returns only safe status classes and next actions; "
        "does not require a clean worktree or modify Git refs."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.read"),
    create_runtime=DiagnoseRemoteAuthTool,
    readiness=remote_readiness,
)
INSPECT_PROJECT_REMOTE: Final = ToolDefinition(
    name="inspect_project_remote",
    description=(
        "Read the authenticated private origin's current GitHub main, local main, tracking main and feature ancestry. "
        "Never fetches, pushes, modifies a ref or publishes a PR; requires the existing host gh authentication."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.read"),
    create_runtime=InspectRemoteTool,
    readiness=remote_readiness,
)
FETCH_PROJECT_REMOTE_MAIN: Final = ToolDefinition(
    name="fetch_project_remote_main",
    description=(
        "Fetch only refs/heads/main into origin/main after exact branch, HEAD and remote SHA preconditions. "
        "No force, reset, checkout, local main update or ref deletion. Check the result before proceeding."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.write"),
    create_runtime=FetchRemoteMainTool,
    readiness=remote_readiness,
)
PUSH_PROJECT_FEATURE: Final = ToolDefinition(
    name="push_project_feature",
    description=(
        "Push only the current clean and remote-main-based topic branch to the same named origin ref. "
        "Requires exact observed local HEAD and remote refs, forbids main, tags, force and hook bypass. "
        "If publication cannot be confirmed, inspect remote refs before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.write"),
    create_runtime=PushFeatureTool,
    readiness=remote_readiness,
)
CREATE_PROJECT_DRAFT_PR: Final = ToolDefinition(
    name="create_project_draft_pr",
    description=(
        "Create at most one draft PR from an already-pushed, exactly verified feature branch to main. "
        "Generate body from committed new .changes and Git commits with optional reviewed purpose and exceptions; "
        "never merge, mark ready, change Rulesets or bypass CI. Recheck existing PRs to avoid duplicates."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "gh.pr.create"),
    create_runtime=CreateFeaturePRTool,
    readiness=remote_readiness,
)
