# Worktree assessments implementation plan

> Execute inline with existing MCP tools; preserve original and historical source branches.

**Goal:** Record an explicit compatibility judgement on a managed topic worktree and commit only that reviewed assessment.
**Architecture:** Two calls separate file preparation from commit. Reuse managed worktree binding, startup access/signing settings and the current commit runtime. No historical config or credentials are loaded.
**Tech Stack:** Existing Python, Git, FastMCP, Pydantic; no new dependency.
**Spec:** ADR 0005/0006 and [worktree contract](../../tools/git/worktrees.md).

## Constraints and decisions
- Original checkout remains on its observed branch/HEAD with the same index and clean state.
- prepare_project_worktree_assessment takes the existing worktree preconditions, assessment_id and explicit assessment_toml (UTF-8 <=16 KiB). It writes only an absent .changes/<id>.toml and returns its SHA-256 digest; it does not stage or commit.
- commit_project_worktree_assessment takes those preconditions, assessment_id, expected_digest and message. Require the single assessment to be the only target change; reject altered bytes, filters changing staged bytes, extra staged files and moved HEAD.
- Existing release Assessment validation is reused. This narrow initial-assessment tool requires supersedes=[]; correction/automatic allocation is outside this change. Never overwrite existing IDs or duplicate an identical judgement under another ID.
- Check source and mapped target path policies; reject symlink parents/files and ignored assessment paths. Do not bypass hooks or signing, reset, stash, delete or auto-retry.
- Failure can leave a file, staged changes or a completed commit. Re-observe worktree and file bytes before retrying.
- Existing MCP workspace editing cannot target ignored worktrees, so bootstrap this capability on a dedicated current-tree topic branch.

## Review focus
Stale refs/digests; partial writes and failed hooks; read-only or denied source paths after mapping; unrelated staged/untracked changes; changed staged bytes or post-commit hook mutations.

## Task 1: Prepare and commit reviewed assessment
- [x] Add real-Git tests in tests/tools/git/test_worktree_assessments.py; run them and observe missing-tool failures.
- [ ] Add shared validation in tools/git/_worktree_assessments.py and one facade per public tool; register/export without changing existing order.
- [ ] Verify original preservation, exact single-path diff/content, strict schema and permission/signing behavior; run selected integration tests to green.
- [ ] Update worktree contract, README tool list and a minor .changes judgement.
- [ ] Run lint, format_check, typecheck, unit and integration; review full change and sign commit.

Further worktree recipe execution is a subsequent capability, not implemented by these two tools.

## Verified progress
- New real-Git integration tests: 27 passed, including SHA-1/SHA-256, controller signing, duplicate judgement rejection, hook failures and byte/mode mutations.
- Independent review: two Important findings reproduced RED then fixed GREEN: physical worktree policy restrictions and post-commit mutations hidden by Git status. No deferred findings.
- The existing PR collector rejected omitted migration despite the release schema's empty default. A failing regression led to applying that same default; explicit invalid values remain rejected.
- Full unit: 540 passed. Full integration: 576 passed, 81 skipped; existing dependency deprecation warnings remain.
- Actual historical runtime branch worktree: ready/clean at 6a1a62af298b9d975b08b54da59da7233e4bc6ae; authenticated remote inspection succeeded without unpublished main contamination. No push/PR was performed.
- Original main, workflow and worktree-feature refs remain unchanged.
- Follow-up: worktree-specific recipe execution must retain the existing shared single-job slot, request recovery and cancellation; the current manager binds its policy/executable paths to the startup workspace.

Final static checks: lint and format_check passed; Pyright 0 errors / 0 warnings. Public PR regression: 15 passed.
