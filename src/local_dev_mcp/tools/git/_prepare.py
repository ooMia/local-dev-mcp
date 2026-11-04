"""Separate startup commit configuration from live per-invocation checks."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastmcp.exceptions import ToolError

from ...errors import PolicyError
from ...readiness import PreparationIssue
from ...settings import Policy
from .. import executable
from ..gpg import configuration
from .errors import GitConfigurationError

if TYPE_CHECKING:
    from ...runtime import ServerSettings


@dataclass(frozen=True)
class PreparedGitCommit:
    name: str = field(repr=False)
    email: str = field(repr=False)
    # None means unsigned by configured intent, never failed signing preparation.
    signing: configuration.GpgSigning | None = field(default=None, repr=False)

    def environment(self) -> dict[str, str]:
        return {
            **(self.signing.environment() if self.signing is not None else {}),
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }


def commit_configuration(values: Mapping[str, str | None], policy: Policy) -> PreparedGitCommit:
    selected: dict[str, str] = {}
    fields: tuple[Literal["GIT_USER_NAME", "GIT_USER_EMAIL"], ...] = ("GIT_USER_NAME", "GIT_USER_EMAIL")
    for key in fields:
        value = values.get(key)
        if value is None or not value.strip():
            raise GitConfigurationError.missing_git_identity(key)
        if value != value.strip() or any(char in value for char in "\0\r\n<>"):
            raise GitConfigurationError.invalid_git_identity(key)
        selected[key] = value
    flag = values.get("GIT_COMMIT_SIGN", "false")
    if flag not in ("true", "false"):
        raise GitConfigurationError.invalid_commit_sign()
    signing = None
    if flag == "true":
        signing = configuration.signing_configuration(values, policy)
    return PreparedGitCommit(selected["GIT_USER_NAME"], selected["GIT_USER_EMAIL"], signing)


async def prepare_git_commit(settings: ServerSettings) -> PreparedGitCommit:
    require_git_access(settings, commit=True)
    prepared = settings.require_commit()
    if prepared.signing is not None:
        await prepared.signing.require_available(dict(settings.environment))
    return prepared


def require_git_access(
    settings: ServerSettings, preparation: PreparedGitCommit | None = None, *, commit: bool = False
) -> None:
    """Authorize internal preparation and execution before accessing host resources."""
    if commit and preparation is None and isinstance(settings.commit, PreparedGitCommit):
        preparation = settings.commit
    try:
        settings.policy.require_project_access()
        for command in settings.commit_required_commands if commit or preparation is not None else ("git",):
            settings.policy.require_command(command)
        if preparation is not None and preparation.signing is not None:
            settings.policy.require_command("gpg")
    except PermissionError as error:
        raise ToolError(str(error)) from None
    settings.require_git()
    if commit or preparation is not None:
        settings.require_commit()


def git_configuration(policy: Policy) -> Path:
    """Resolve Git independently of commit identity and signing settings."""
    try:
        policy.require_command("git")
    except PermissionError:
        raise PolicyError.command_denied("git") from None
    return executable("git")


def git_readiness(settings: ServerSettings) -> PreparationIssue | None:
    return settings.git if isinstance(settings.git, PreparationIssue) else None


def commit_readiness(settings: ServerSettings) -> PreparationIssue | None:
    return settings.commit if isinstance(settings.commit, PreparationIssue) else None
