"""Project tools served through FastMCP's typed, strict invocation interface."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib import metadata
from typing import Final

from fastmcp import FastMCP

from ..app.runtime import ServerSettings
from ..diagnostics import warn_preparation
from ..settings import Policy
from ..tools import client_elicitation, documents, get_tool_readiness, git, indexing, release, tasks, workspace
from . import registry

TOOLS: Final[tuple[registry.ToolDefinition[ServerSettings], ...]] = (
    git.GET_PROJECT_HEAD,
    git.GET_MAIN_HEAD,
    git.GET_PROJECT_STATUS,
    git.GET_PROJECT_DIFF,
    git.GET_PROJECT_REF,
    git.INSPECT_PROJECT_COMMIT_HISTORY,
    git.INSPECT_PROJECT_POLICY,
    git.COMPARE_PROJECT_REFS,
    git.CONSOLIDATE_PROJECT_HISTORY,
    git.STAGE_PROJECT_PATHS,
    git.UNSTAGE_PROJECT_PATHS,
    git.COMMIT_PROJECT_CHANGES,
    git.CREATE_PROJECT_BRANCH,
    git.MERGE_PROJECT_BRANCH,
    git.CREATE_PROJECT_BRANCH_FROM_REF,
    git.SWITCH_PROJECT_BRANCH,
    git.GET_PROJECT_PICK_STATE,
    git.CHERRY_PICK_PROJECT_COMMIT,
    git.GET_PROJECT_WORKTREE_PICK_STATE,
    git.CHERRY_PICK_PROJECT_WORKTREE_COMMIT,
    git.COMMIT_PROJECT_WORKTREE_PICK,
    git.RESTORE_PROJECT_PICK_PATHS,
    git.DIAGNOSE_PROJECT_REMOTE_AUTH,
    git.INSPECT_PROJECT_REMOTE,
    git.FETCH_PROJECT_REMOTE_MAIN,
    git.PUSH_PROJECT_FEATURE,
    git.CREATE_PROJECT_DRAFT_PR,
    git.GET_PROJECT_PR_MERGE_PLAN,
    git.GET_PROJECT_PR_STATUS,
    git.MARK_PROJECT_PR_READY,
    git.MERGE_PROJECT_PR,
    git.PREPARE_PROJECT_WORKTREE,
    git.FINALIZE_PROJECT_WORKTREE,
    git.INSPECT_PROJECT_WORKTREE,
    git.INSPECT_PROJECT_BRANCH_REMOTE,
    git.FETCH_PROJECT_BRANCH_REMOTE_MAIN,
    git.PUSH_PROJECT_BRANCH,
    git.CREATE_PROJECT_BRANCH_DRAFT_PR,
    git.PREPARE_PROJECT_WORKTREE_ASSESSMENT,
    git.COMMIT_PROJECT_WORKTREE_ASSESSMENT,
    git.START_PROJECT_WORKTREE_TASK,
    git.CREATE_PROJECT_WORKTRUNK,
    git.INSPECT_PROJECT_WORKTRUNK,
    git.LIST_PROJECT_WORKTREES,
    git.GET_PROJECT_WORKTREE_REVIEW,
    git.PLAN_PROJECT_WORKTREE_CLEANUP,
    git.VERIFY_PROJECT_WORKTRUNK_QUALITY,
    git.GET_PROJECT_WORKTRUNK_MERGE_PLAN,
    git.REMOVE_PROJECT_WORKTRUNK,
    release.GET_RELEASE_PLAN,
    release.PREPARE_PROJECT_RELEASE,
    get_tool_readiness.GET_TOOL_READINESS,
    indexing.GET_PROJECT_INDEX_STATUS,
    indexing.REFRESH_PROJECT_INDEX,
    client_elicitation.DIAGNOSE_CLIENT_ELICITATION,
    client_elicitation.PROBE_CLIENT_ELICITATION,
    client_elicitation.PROBE_CLIENT_ELICITATION_MRTR,
    tasks.LIST_PROJECT_TASKS,
    tasks.START_PROJECT_TASK,
    tasks.GET_PROJECT_TASK,
    tasks.CANCEL_PROJECT_TASK,
    workspace.CREATE_WORKSPACE_FILE,
    workspace.DELETE_WORKSPACE_FILE,
    workspace.REPLACE_WORKSPACE_FILE,
    workspace.LIST_WORKSPACE_ENTRIES,
    workspace.FIND_WORKSPACE_PATHS,
    workspace.SEARCH_WORKSPACE_TEXT,
    workspace.READ_WORKSPACE_FILE,
    documents.DIAGNOSE_DOCUMENTS,
)


def load_settings(policy: Policy) -> ServerSettings:
    settings = ServerSettings.load(policy)
    settings = replace(settings, readiness=registry.tool_readiness(TOOLS, settings))
    # Recipe executables and permission readiness belong to the startup snapshot.
    settings.tasks
    return settings


def create_server(policy: Policy) -> FastMCP:
    settings = load_settings(policy)
    warn_preparation(tuple(state.issue for state in settings.readiness if state.issue is not None))

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            try:
                await settings.tasks.close()
            finally:
                try:
                    await settings.index.close()
                finally:
                    settings.files.clear()

    server = FastMCP(
        "local-dev-mcp",
        version=metadata.version("local-dev-mcp"),
        strict_input_validation=True,
        mask_error_details=True,
        on_duplicate="error",
        tasks=False,
        lifespan=lifespan,
        middleware=[registry.SafeToolDiagnostics()],
        instructions=(
            "Use get_tool_readiness to inspect fixed startup preparation, not live execution health. "
            "diagnose_documents uses repository_id=local-dev-mcp and reports Blocked pending relation approval. "
            "Explore with directory listings or glob path search; literal text search defaults to paths only. "
            "Read only needed line ranges. Inspect complete/reason/skipped and continuation positions. "
            "File results live in structuredContent; source is not duplicated in text blocks. "
            "read_ref is server/workspace-local, expires or can be evicted, and is not a Chat identity. "
            "Use replace_workspace_file for one observed range; inspect outcome and next_call before continuing. "
            "On conflict read the current file and reconstruct the edit; never replay blindly. "
            "Create absent text files with create_workspace_file; enable missing parent creation explicitly if needed. "
            "Delete one observed file with delete_workspace_file only when its contents can be discarded. "
            "Create/delete use request_id for lost-response recovery; replayed receipts are historical outcomes. "
            "Use list_project_tasks and start_project_task for registered lint, format and test CLI jobs. "
            "Select tests explicitly; all_tests=true requests the configured whole profile. Reuse request_id only "
            "to recover the same submission, then get/cancel its job_id. Do not edit inputs while a job runs. "
            "Task calls omit diagnostic bodies by default; get_project_task(include_output=true) reads retained "
            "masked output without running the task again. "
            "Local project Git tools: inspect HEAD, status and diff; stage, unstage and commit explicit changes. "
            "Before main integration inspect_project_policy checks observed local refs and assessments only; "
            "this read-only preflight cannot replace remote PR, exact-head CI, or provenance checks. "
            "Commit records the entire current index. Writes may have taken effect even when an error is reported; "
            "inspect status and HEAD before retrying. "
            "Branch creation/switching requires the observed HEAD and branch and may carry dirty edits. "
            "Use get_release_plan to inspect committed impact and recommendation; prepare_project_release requires "
            "matching HEAD, digest, version and clean release branch. It never commits or publishes. "
            "Scoped remote inspection, main fetch, feature push and Draft PR require gh auth and separate permissions. "
            "Preview an exact PR squash message with get_project_pr_merge_plan; merge_project_pr separately requires "
            "gh.pr.merge and the reviewed head/digest. Only merged_verified confirms the actual merge message. "
            "Use get_project_pr_status for exact-head CI, then mark_project_pr_ready with independent gh.pr.ready. "
            "CI success is an orchestration precondition, not enforced inside merge_project_pr. "
            "Remote tools never force-push or bypass gates; "
            "only merge_project_pr can update remote main through a conditional PR squash. No pull or stash; "
            "merge_project_branch requires assessed squash for opted-in workflows "
            "and otherwise guarded main fast-forward."
        ),
    )
    registry.register_tools(server, policy, TOOLS, settings, readiness=settings.readiness)
    return server
