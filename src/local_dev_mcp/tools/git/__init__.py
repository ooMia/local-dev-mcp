"""Supported Git definitions and startup configuration interface."""

from ._environment import command_environment
from ._prepare import PreparedGitCommit, commit_configuration, git_configuration
from .facades.commit_project_changes import COMMIT_PROJECT_CHANGES
from .facades.get_project_diff import GET_PROJECT_DIFF
from .facades.get_project_head import GET_PROJECT_HEAD
from .facades.get_project_status import GET_PROJECT_STATUS
from .facades.stage_project_paths import STAGE_PROJECT_PATHS
from .facades.unstage_project_paths import UNSTAGE_PROJECT_PATHS

__all__ = [
    "COMMIT_PROJECT_CHANGES",
    "GET_PROJECT_DIFF",
    "GET_PROJECT_HEAD",
    "GET_PROJECT_STATUS",
    "STAGE_PROJECT_PATHS",
    "UNSTAGE_PROJECT_PATHS",
    "PreparedGitCommit",
    "command_environment",
    "commit_configuration",
    "git_configuration",
]
