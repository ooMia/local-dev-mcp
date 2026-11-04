"""Git-specific startup diagnostics; validation remains in Git configuration."""

from typing import Literal, Self

from ...errors import ConfigurationError


class GitConfigurationError(ConfigurationError):
    # Commit identity: the field name is safe; its configured value is never accepted.
    @classmethod
    def missing_git_identity(cls, name: Literal["GIT_USER_NAME", "GIT_USER_EMAIL"]) -> Self:
        guide = "git config get user.name" if name == "GIT_USER_NAME" else "git config get user.email"
        return cls.environment_value(name, "Required commit identity is missing.", f"Check `{guide}`.")

    @classmethod
    def invalid_git_identity(cls, name: Literal["GIT_USER_NAME", "GIT_USER_EMAIL"]) -> Self:
        guide = "git config get user.name" if name == "GIT_USER_NAME" else "git config get user.email"
        return cls.environment_value(
            name, "Identity must have no surrounding whitespace, NUL, CR, LF or angle brackets.", f"Check `{guide}`."
        )

    # Commit intent: omission has a default, but an explicitly invalid flag is an error.
    @classmethod
    def invalid_commit_sign(cls) -> Self:
        return cls.environment_value(
            "GIT_COMMIT_SIGN", "Expected exactly true or false.", "Choose whether MCP commits must be signed."
        )
