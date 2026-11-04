"""Exercise nullable raw settings through the Git startup configuration interface."""

from pathlib import Path

import pytest

from local_dev_mcp.errors import ConfigurationError, ExitCode
from local_dev_mcp.settings import Policy, read_dotenv
from local_dev_mcp.tools.git._prepare import commit_configuration
from local_dev_mcp.tools.git.errors import GitConfigurationError


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("", "Required commit identity is missing"),
        ("GIT_USER_NAME", "Required commit identity is missing"),
        ("GIT_USER_NAME=", "Required commit identity is missing"),
        ('GIT_USER_NAME="   "', "Required commit identity is missing"),
        ('GIT_USER_NAME=" <private-canary> "', "Identity must have no surrounding whitespace"),
    ],
)
def test_missing_or_invalid_identity_never_enters_snapshot(tmp_path: Path, line: str, reason: str) -> None:
    (tmp_path / ".env").write_text(f"{line}\nGIT_USER_EMAIL=test@example.invalid\n")
    with pytest.raises(GitConfigurationError) as caught:
        commit_configuration(read_dotenv(tmp_path), Policy(tmp_path, (tmp_path,), ("git",)))
    assert isinstance(caught.value, ConfigurationError)
    assert caught.value.code == ExitCode.CONFIG_ERROR
    assert reason in str(caught.value)
    assert "git config get user.name" in str(caught.value)
    assert "private-canary" not in str(caught.value)


@pytest.mark.parametrize("line", ["GIT_COMMIT_SIGN", "GIT_COMMIT_SIGN=", "GIT_COMMIT_SIGN=private-canary"])
def test_explicit_invalid_signing_flag_does_not_default_to_unsigned(tmp_path: Path, line: str) -> None:
    (tmp_path / ".env").write_text(f"GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n{line}\n")
    with pytest.raises(GitConfigurationError) as caught:
        commit_configuration(read_dotenv(tmp_path), Policy(tmp_path, (tmp_path,), ("git",)))
    assert caught.value.code == ExitCode.CONFIG_ERROR
    assert caught.value.target == "GIT_COMMIT_SIGN"
    assert "Expected exactly true or false" in str(caught.value)
    assert "private-canary" not in str(caught.value)
