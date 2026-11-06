"""Supported Git definitions and startup configuration interface."""

from ._environment import command_environment
from ._prepare import PreparedGitCommit, commit_configuration, git_configuration
from .facades.cherry_pick_project_commit import CHERRY_PICK_PROJECT_COMMIT
from .facades.commit_project_changes import COMMIT_PROJECT_CHANGES
from .facades.compare_project_refs import COMPARE_PROJECT_REFS
from .facades.create_project_branch import CREATE_PROJECT_BRANCH
from .facades.create_project_branch_from_ref import CREATE_PROJECT_BRANCH_FROM_REF
from .facades.get_main_head import GET_MAIN_HEAD
from .facades.get_project_diff import GET_PROJECT_DIFF
from .facades.get_project_head import GET_PROJECT_HEAD
from .facades.get_project_pick_state import GET_PROJECT_PICK_STATE
from .facades.get_project_ref import GET_PROJECT_REF
from .facades.get_project_status import GET_PROJECT_STATUS
from .facades.merge_project_branch import MERGE_PROJECT_BRANCH
from .facades.restore_project_pick_paths import RESTORE_PROJECT_PICK_PATHS
from .facades.stage_project_paths import STAGE_PROJECT_PATHS
from .facades.switch_project_branch import SWITCH_PROJECT_BRANCH
from .facades.unstage_project_paths import UNSTAGE_PROJECT_PATHS

__all__ = [
    "CHERRY_PICK_PROJECT_COMMIT",
    "COMMIT_PROJECT_CHANGES",
    "COMPARE_PROJECT_REFS",
    "CREATE_PROJECT_BRANCH",
    "CREATE_PROJECT_BRANCH_FROM_REF",
    "GET_MAIN_HEAD",
    "GET_PROJECT_DIFF",
    "GET_PROJECT_HEAD",
    "GET_PROJECT_REF",
    "GET_PROJECT_PICK_STATE",
    "GET_PROJECT_STATUS",
    "MERGE_PROJECT_BRANCH",
    "RESTORE_PROJECT_PICK_PATHS",
    "STAGE_PROJECT_PATHS",
    "SWITCH_PROJECT_BRANCH",
    "UNSTAGE_PROJECT_PATHS",
    "PreparedGitCommit",
    "command_environment",
    "commit_configuration",
    "git_configuration",
]
