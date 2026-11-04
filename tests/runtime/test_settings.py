from pathlib import Path

import pytest

from local_dev_mcp.settings import ConfigurationError, Policy
from tests.support.git import WorkspaceConfigFactory


def test_project_path_permission_does_not_require_git(tmp_path: Path, stdio_workspace: WorkspaceConfigFactory) -> None:
    stdio_workspace(commands="[]")
    policy = Policy.load(tmp_path)
    policy.require_project_access()
    with pytest.raises(PermissionError, match="git is not allowed"):
        policy.require_command("git")


def test_config_does_not_search_parent(tmp_path: Path, stdio_workspace: WorkspaceConfigFactory) -> None:
    stdio_workspace()
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(ConfigurationError):
        Policy.load(child)
