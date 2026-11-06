# Managed worktree check execution plan

> Execute inline; preserve the controller checkout and earlier source branches.

**Goal:** Run registered check recipes against an observed managed branch without changing the original checkout.
**Architecture:** Add start_project_worktree_task and bind its pinned worktree to the existing shared TaskManager. Retain one server-wide active slot, request tombstones, get/cancel tools, startup executables/environment and redaction. A small internal TaskWorktree record supplies pre/post Git validation; no historical config is loaded.
**Spec:** ADR 0005/0006, [task contract](../../tools/tasks/README.md), [worktree contract](../../tools/git/worktrees.md).
**Stack:** Existing Git, Python, FastMCP and shellous. No new dependency.

## Scope and choices
- Inputs: task, request_id, branch, expected_branch_head, expected_branch, expected_head, targets=null, all_tests=false.
- git.worktree.write plus the selected recipe's existing permission are required. Reject format recipes; lint/test recipes may still cause side effects and are not sandboxed.
- Same manager/slot/retention for controller and worktree jobs. Bind request identity to branch, both SHAs and original branch; replay returns historical job metadata without revalidating moved refs or rerunning a process.
- Serialize submission preparation to prevent races across async preflight. Recheck closing/active state after asynchronous preflight.
- Validate worktree ownership and exact refs immediately before process start, then again on completion/cancellation. Original branch/HEAD/index and both clean states must match.
- Startup recipe/executable/environment are frozen. Never read target config.toml or .env; caller cannot supply cwd, commands or free options.
- Test selectors obey both mapped source and physical target path grants/denials.
- Optional worktree result metadata reports pinned context and verified=true/false/null independently of exit_code. A zero exit plus changed/unverified Git state cannot be a succeeded job.
- Retain partial changes and diagnostics. Never reset, stash, auto-commit, auto-retry or publish.

## Review focus
Concurrent submissions during preflight; lost responses after refs move; cancellation and close during verification; dirty/moved targets; source/physical-target path restrictions and default task compatibility.

## Task 1: Shared worktree-aware jobs
- [x] Add real-Git/process tests in tests/tools/git/test_worktree_tasks.py; observe missing-tool failures.
- [x] Add the internal task-worktree record and optional result context; extend the existing manager without creating another scheduler.
- [x] Add/register/export start_project_worktree_task; reuse existing get/cancel.
- [x] Verify shared-slot exclusion, stable replay, startup environment, selectors, changed-state failure, cancellation and strict schema.
- [x] Update task/worktree contracts, README and a minor assessment.
- [x] Run lint/format/typecheck plus complete unit/integration; independent review; sign commit.

The current MCP lacks a workspace switch for edits, so bootstrap this capability on a new branch at the same controller tree.

## Verification record
- Initial missing-tool run: 6 failed as expected; invalid-input checks did not establish implementation coverage.
- Shared old/new task tests: 32 passed. Added strict MCP schema coverage is included in the full regression.
- A deterministic delayed-verification test exposed premature succeeded state and ineffective cancellation. Both failed RED, then passed GREEN after keeping the job running and retaining the slot until verification completes.
- Full unit: 540 passed. Full integration: 591 passed, 81 skipped; existing dependency deprecation warnings remain.
- Lint and format_check passed. Pyright: 0 errors, 0 warnings.
- Independent read-only review found no Critical, Important or Minor issues; no findings deferred.
- Original main and feat/worktree-assessments refs were unchanged; the historical runtime worktree remained ready/clean.
- Actual private-branch check execution and PR publication remain acceptance steps after activating the new tool; no remote publication was performed.
