"""Supported Git definitions and startup configuration interface."""

from ._environment import command_environment
from ._prepare import PreparedGitCommit, commit_configuration, git_configuration
from .facades.cherry_pick_project_commit import CHERRY_PICK_PROJECT_COMMIT
from .facades.commit_project_changes import COMMIT_PROJECT_CHANGES
from .facades.commit_project_worktree_assessment import COMMIT_PROJECT_WORKTREE_ASSESSMENT
from .facades.compare_project_refs import COMPARE_PROJECT_REFS
from .facades.consolidate_project_history import CONSOLIDATE_PROJECT_HISTORY
from .facades.create_project_branch import CREATE_PROJECT_BRANCH
from .facades.create_project_branch_from_ref import CREATE_PROJECT_BRANCH_FROM_REF
from .facades.finalize_project_worktree import FINALIZE_PROJECT_WORKTREE
from .facades.get_main_head import GET_MAIN_HEAD
from .facades.get_project_diff import GET_PROJECT_DIFF
from .facades.get_project_head import GET_PROJECT_HEAD
from .facades.get_project_pick_state import GET_PROJECT_PICK_STATE
from .facades.get_project_pr_merge_plan import GET_PROJECT_PR_MERGE_PLAN
from .facades.get_project_pr_status import GET_PROJECT_PR_STATUS
from .facades.get_project_ref import GET_PROJECT_REF
from .facades.get_project_status import GET_PROJECT_STATUS
from .facades.get_project_worktrunk_merge_plan import GET_PROJECT_WORKTRUNK_MERGE_PLAN
from .facades.inspect_project_commit_history import INSPECT_PROJECT_COMMIT_HISTORY
from .facades.inspect_project_policy import INSPECT_PROJECT_POLICY
from .facades.inspect_project_worktree import INSPECT_PROJECT_WORKTREE
from .facades.mark_project_pr_ready import MARK_PROJECT_PR_READY
from .facades.merge_project_branch import MERGE_PROJECT_BRANCH
from .facades.merge_project_pr import MERGE_PROJECT_PR
from .facades.prepare_project_worktree import PREPARE_PROJECT_WORKTREE
from .facades.prepare_project_worktree_assessment import PREPARE_PROJECT_WORKTREE_ASSESSMENT
from .facades.publish_project_branch import (
    CREATE_PROJECT_BRANCH_DRAFT_PR,
    INSPECT_PROJECT_BRANCH_REMOTE,
    PUSH_PROJECT_BRANCH,
)
from .facades.remote_pr import (
    CREATE_PROJECT_DRAFT_PR,
    DIAGNOSE_PROJECT_REMOTE_AUTH,
    FETCH_PROJECT_REMOTE_MAIN,
    INSPECT_PROJECT_REMOTE,
    PUSH_PROJECT_FEATURE,
)
from .facades.restore_project_pick_paths import RESTORE_PROJECT_PICK_PATHS
from .facades.stage_project_paths import STAGE_PROJECT_PATHS
from .facades.start_project_worktree_task import START_PROJECT_WORKTREE_TASK
from .facades.switch_project_branch import SWITCH_PROJECT_BRANCH
from .facades.unstage_project_paths import UNSTAGE_PROJECT_PATHS
from .facades.verify_project_worktrunk_quality import VERIFY_PROJECT_WORKTRUNK_QUALITY
from .facades.worktrunk import CREATE_PROJECT_WORKTRUNK, INSPECT_PROJECT_WORKTRUNK, REMOVE_PROJECT_WORKTRUNK

__all__ = [
    "CONSOLIDATE_PROJECT_HISTORY",
    "CREATE_PROJECT_WORKTRUNK",
    "INSPECT_PROJECT_WORKTRUNK",
    "VERIFY_PROJECT_WORKTRUNK_QUALITY",
    "GET_PROJECT_WORKTRUNK_MERGE_PLAN",
    "REMOVE_PROJECT_WORKTRUNK",
    "START_PROJECT_WORKTREE_TASK",
    "PREPARE_PROJECT_WORKTREE_ASSESSMENT",
    "COMMIT_PROJECT_WORKTREE_ASSESSMENT",
    "PREPARE_PROJECT_WORKTREE",
    "FINALIZE_PROJECT_WORKTREE",
    "INSPECT_PROJECT_WORKTREE",
    "INSPECT_PROJECT_BRANCH_REMOTE",
    "PUSH_PROJECT_BRANCH",
    "CREATE_PROJECT_BRANCH_DRAFT_PR",
    "CHERRY_PICK_PROJECT_COMMIT",
    "COMMIT_PROJECT_CHANGES",
    "COMPARE_PROJECT_REFS",
    "CREATE_PROJECT_BRANCH",
    "CREATE_PROJECT_BRANCH_FROM_REF",
    "GET_MAIN_HEAD",
    "GET_PROJECT_DIFF",
    "GET_PROJECT_HEAD",
    "GET_PROJECT_REF",
    "INSPECT_PROJECT_COMMIT_HISTORY",
    "INSPECT_PROJECT_POLICY",
    "GET_PROJECT_PICK_STATE",
    "GET_PROJECT_STATUS",
    "MERGE_PROJECT_BRANCH",
    "GET_PROJECT_PR_MERGE_PLAN",
    "GET_PROJECT_PR_STATUS",
    "MARK_PROJECT_PR_READY",
    "MERGE_PROJECT_PR",
    "RESTORE_PROJECT_PICK_PATHS",
    "INSPECT_PROJECT_REMOTE",
    "FETCH_PROJECT_REMOTE_MAIN",
    "PUSH_PROJECT_FEATURE",
    "CREATE_PROJECT_DRAFT_PR",
    "DIAGNOSE_PROJECT_REMOTE_AUTH",
    "STAGE_PROJECT_PATHS",
    "SWITCH_PROJECT_BRANCH",
    "UNSTAGE_PROJECT_PATHS",
    "PreparedGitCommit",
    "command_environment",
    "commit_configuration",
    "git_configuration",
]
