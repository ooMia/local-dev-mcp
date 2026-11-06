"""Select startup workspace policy without changing the caller's working directory."""

import argparse
from pathlib import Path

from ..errors import LocalIOError
from ..settings import Policy


def _path(value: str) -> Path:
    if not value or "\0" in value:
        raise argparse.ArgumentTypeError("Workspace path must be nonempty and contain no NUL characters.")
    return Path(value)


def add_workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        type=_path,
        default=None,
        metavar="PATH",
        help="Work in PATH; relative paths use the invocation directory. Defaults to the current directory.",
    )


def add_policy_edit_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-policy-edits",
        action="store_true",
        help="Allow direct config.toml/.gitignore edits; all other path and edit checks still apply.",
    )


def load_workspace_policy(workspace: Path | None, *, allow_policy_edits: bool = False) -> Policy:
    if workspace is None:
        try:
            workspace = Path.cwd()
        except OSError as error:
            raise LocalIOError.locate_project(error) from error
    return Policy.load(workspace, allow_policy_edits=allow_policy_edits)
