"""Commit the current index using startup identity and explicit signing intent."""

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
from ..commands import commit, diff, show, var
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
            prepared: PreparedGitCommit = Depends(provide),
        ) -> ProjectCommit:
            return await self.commit(message, prepared)

        return invoke

    async def commit(self, message: str, prepared: PreparedGitCommit) -> ProjectCommit:
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
        before = await _repository.read_head(repo)
        await var.require_commit_identity(repo)
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
        "No auto-stage, amend or hook bypass; failed signing never falls back to unsigned."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=commit_commands,
    create_runtime=CommitProjectChangesTool,
    readiness=commit_readiness,
)
