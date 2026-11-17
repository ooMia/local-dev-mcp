"""Native and Worktrunk share Git worktree authorization without relaxing wt access."""

from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.tools.git import _worktrees
from local_dev_mcp.tools.git.worktrunk.adapter import require_worktrunk_access
from tests.support.worktrees import policy

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("")


def test_native_access_does_not_implicitly_allow_worktrunk(tmp_path: Path) -> None:
    native = ServerSettings.load(policy(tmp_path))
    _worktrees.require_permission(native, write=True)
    with pytest.raises(ToolError, match="System command wt is not allowed"):
        require_worktrunk_access(native, write=True)

    permitted = replace(policy(tmp_path), allowed_commands=("git", "wt", "git.worktree.write"))
    require_worktrunk_access(ServerSettings.load(permitted), write=True)


@pytest.mark.parametrize(
    ("commands", "missing"),
    [
        (("wt", "git.worktree.write"), "git"),
        (("git", "git.worktree.write"), "wt"),
        (("git", "wt"), "git.worktree.write"),
        (("git",), "wt"),
    ],
)
def test_worktrunk_write_checks_commands_in_existing_order(
    tmp_path: Path, commands: tuple[str, ...], missing: str
) -> None:
    settings = ServerSettings.load(replace(policy(tmp_path), allowed_commands=commands))
    with pytest.raises(ToolError, match=f"System command {missing} is not allowed"):
        require_worktrunk_access(settings, write=True)


def test_worktrunk_read_does_not_require_write_permission(tmp_path: Path) -> None:
    settings = ServerSettings.load(replace(policy(tmp_path), allowed_commands=("git", "wt")))
    require_worktrunk_access(settings, write=False)
    with pytest.raises(ToolError, match=r"System command git\.worktree\.write is not allowed"):
        require_worktrunk_access(settings, write=True)


def test_both_backends_reject_disallowed_workspace(tmp_path: Path) -> None:
    settings = replace(
        ServerSettings.load(policy(tmp_path)),
        policy=replace(
            policy(tmp_path),
            allowed_paths=(tmp_path.parent / "unrelated-project",),
            allowed_commands=("git", "wt", "git.worktree.write"),
        ),
    )
    with pytest.raises(ToolError, match="Project directory is not allowed"):
        _worktrees.require_permission(settings, write=True)
    with pytest.raises(ToolError, match="Project directory is not allowed"):
        require_worktrunk_access(settings, write=True)
