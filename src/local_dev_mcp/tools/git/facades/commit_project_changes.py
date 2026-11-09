"""Commit the current index using startup identity and explicit signing intent."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition, safe_execution
from .._execution import GitExecution
from .._prepare import PreparedGitCommit, commit_readiness, prepare_git_commit
from ..commands import commit, diff, revisions, show, status, var
from . import _repository, _visibility
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class ProjectCommit(TypedDict):
    project: str
    success: bool
    head_before: str | None
    head_after: str | None
    head_observed: bool
    observation_error: str | None
    signing_requested: bool
    signature_verified: bool | None
    signature_error: str | None
    observed_at: str


type CommitMessage = Annotated[
    str,
    Field(
        min_length=1,
        description="Commit message for the entire current index. Multiline allowed; blank-only and NUL rejected.",
    ),
]


@dataclass(frozen=True)
class CommitProjectChangesTool:
    settings: ServerSettings

    @property
    def execute(self) -> Callable[..., Awaitable[ProjectCommit]]:
        # Bind once per server runtime; no global provider settings or request state.
        @safe_execution
        async def provide() -> PreparedGitCommit:
            return await prepare_git_commit(self.settings)

        async def invoke(
            message: CommitMessage,
            expected_branch: str | None = None,
            expected_head: str | None = None,
            expected_index_fingerprint: str | None = None,
            expected_staged_paths: _repository.ProjectPaths | None = None,
            prepared: PreparedGitCommit = Depends(provide),
        ) -> ProjectCommit:
            return await self.commit(
                message,
                prepared,
                expected_branch=expected_branch,
                expected_head=expected_head,
                expected_index_fingerprint=expected_index_fingerprint,
                expected_staged_paths=expected_staged_paths,
            )

        return invoke

    async def commit(
        self,
        message: str,
        prepared: PreparedGitCommit,
        *,
        expected_branch: str | None = None,
        expected_head: str | None = None,
        expected_index_fingerprint: str | None = None,
        expected_staged_paths: list[str] | None = None,
    ) -> ProjectCommit:
        if not message.strip() or "\0" in message:
            raise ToolError("Provide a nonempty commit message without NUL characters.")
        repo = GitExecution(self.settings, prepared)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        changes = await diff.changed_paths(repo, ["."], staged=True)
        if any(not _visibility.readable(repo, path) for change in changes for path in change):
            raise ToolError(
                "The index contains protected or unreadable paths. No commit was attempted. "
                "Unstage those paths or review the access policy before committing."
            )
        guarded = any(
            item is not None
            for item in (expected_branch, expected_head, expected_index_fingerprint, expected_staged_paths)
        )
        expected_paths: set[str] = set()
        if guarded:
            if any(
                item is None
                for item in (expected_branch, expected_head, expected_index_fingerprint, expected_staged_paths)
            ):
                raise ToolError("Expected branch, HEAD, index fingerprint and staged paths must be supplied together.")
            if (
                not isinstance(expected_index_fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_index_fingerprint) is None
            ):
                raise ToolError("Expected index fingerprint must be the SHA-256 value from get_project_pick_state.")
            if expected_staged_paths is None or not expected_staged_paths:
                raise ToolError("Expected staged paths must name the complete exact staged file set.")
            checked = _repository.check_paths(repo, expected_staged_paths)
            if "." in checked or len(set(checked)) != len(checked):
                raise ToolError("Expected staged paths must be distinct file names, not workspace-wide selections.")
            expected_paths = set(checked)
            actual_paths = {path for change in changes for path in change}
            if actual_paths != expected_paths:
                raise ToolError(
                    "Actual staged paths differ from expected staged paths. Re-read staged diff and status."
                )
            if any(not _visibility.readable(repo, path) for path in expected_paths):
                raise ToolError("Expected staged paths include restricted entries.")
        before = await _repository.read_head(repo)
        await var.require_commit_identity(repo)
        if guarded:
            # Preconditions are checked again immediately before starting Git
            # commit; this is not a transaction against concurrent OS writers or hooks.
            branch, head, _ = await status.read_porcelain_v2(repo)
            if (
                branch != expected_branch
                or head != before
                or head != (None if expected_head == "unborn" else expected_head)
            ):
                raise ToolError("Git branch or HEAD changed. Re-read get_project_pick_state.")
            if await revisions.index_fingerprint(repo) != expected_index_fingerprint:
                raise ToolError("Git index changed. Re-read get_project_pick_state.")
            current = await diff.changed_paths(repo, ["."], staged=True)
            if {path for change in current for path in change} != expected_paths:
                raise ToolError("Staged paths changed. Re-read staged diff before committing.")
        await commit.commit_index(repo, message, signing=prepared.signing is not None)
        try:
            after = await _repository.read_head(repo)
            observed = after is not None
            observation_error = (
                None if observed else "Commit succeeded, but no HEAD commit was observed. Inspect repository state."
            )
        except ToolError:
            after, observed = None, False
            observation_error = (
                "Commit succeeded, but the subsequent HEAD lookup failed. Inspect repository state before retrying."
            )
        verified: bool | None = False if prepared.signing is not None else None
        signature_error = (
            "Commit succeeded, but no HEAD was available for signature verification. Inspect repository state."
        )
        if prepared.signing is None:
            signature_error = None
        elif after is not None:
            try:
                verified = await show.verify_signature(repo, after, prepared.signing.fingerprint)
                signature_error = (
                    None
                    if verified
                    else (
                        "Commit succeeded, but its observed signature is invalid, unavailable "
                        "or does not match the configured key. "
                        "Inspect the commit before retrying."
                    )
                )
            except ToolError, UnicodeError:
                signature_error = (
                    "Commit succeeded, but signature verification could not be completed. "
                    "Inspect the commit before retrying."
                )
        return {
            "project": str(repo.root),
            "success": True,
            "head_before": before,
            "head_after": after,
            "head_observed": observed,
            "observation_error": observation_error,
            "signing_requested": prepared.signing is not None,
            "signature_verified": verified,
            "signature_error": signature_error,
            "observed_at": observed_at(),
        }


def commit_commands(settings: ServerSettings) -> tuple[str, ...]:
    return settings.commit_required_commands


COMMIT_PROJECT_CHANGES: Final = ToolDefinition(
    name="commit_project_changes",
    description=(
        "Commit the entire current index with a message, including preexisting staged changes. "
        "Uses startup identity and signing settings. Unsigned by default; "
        "enabled OpenPGP signing may start shared GnuPG daemons; input prompts are disabled. "
        "Reports commit creation separately from local signature verification. "
        "Staged unreadable paths prevent commit; unstage them or review the access policy. "
        "Optional guarded mode requires expected_branch, expected_head, expected_index_fingerprint and "
        "expected_staged_paths together; checks the exact index content and staged names against observed state. "
        "For an unborn branch use expected_head='unborn'. Preconditions are not an atomic transaction with hooks "
        "or concurrent Git processes. No auto-stage, amend, hook bypass or unsigned retry."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=commit_commands,
    create_runtime=CommitProjectChangesTool,
    readiness=commit_readiness,
)
