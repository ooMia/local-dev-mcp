# Isolated PR worktrees implementation plan

> **For agentic workers:** Execute inline with superpowers:executing-plans; preserve the existing workspace.

**Goal:** Prepare a verified existing feature branch in an isolated linked worktree and publish that branch without switching the original checkout.
**Architecture:** Fixed Git worktree commands own parsing and materialization. A shared worktree resolver verifies identity, common repository and startup policy before existing remote-publication runtimes are reused.
**Tech Stack:** Existing Python, Git, FastMCP and shellous; no new dependency.
**Spec:** Current constraints in [Git contract](../../tools/git/README.md), [remote contract](../../tools/git/remote-pr.md), ADR 0005 and ADR 0006; requested isolated preparation and branch publication.

## Global constraints

- Preserve original branch, HEAD, tracked tree and index. Never reset, stash, force-push, bypass hooks or update main.
- Existing branches only, pinned by exact SHA. Managed paths are deterministic under ignored `.lab/worktrees/`; caller-supplied paths are forbidden.
- Separate `git.worktree.write` permission for preparation. Remote operations retain their existing permissions.
- Never read policy, authentication or recipes from historical worktrees. Never copy .env or credential files.
- Partial failures retain state for inspection. Existing paths are not adopted, removed or overwritten.
- Evaluation allocation and worktree editing/task execution are separate future capabilities. Publication still requires committed .changes; this change cannot silently invent or copy assessments.

## Review focus

- Occupied/colliding/symlink paths and externally registered worktrees.
- Dirty or moved target refs; stale current checkout or remote refs.
- Partial checkout/hook failures and response loss.
- Subtree grants/denials and policy mapping into another checkout.
- Publication runs in the target worktree and preserves original checkout.

### Task 1: Worktree preparation and observation

Files: create `tools/git/commands/worktree.py`, `tools/git/_worktrees.py`,
`tools/git/facades/prepare_project_worktree.py`, `tools/git/facades/inspect_project_worktree.py`;
update Git exports and server registrations.
Interfaces: `prepare_project_worktree(branch, expected_branch_head, expected_branch, expected_head)`;
`inspect_project_worktree(branch)`. The inspector returns absent, partial or ready state with clean/head observations.
- [x] Add real temporary-Git tests for original preservation, occupied branch, SHA mismatch, dirty target, ignore requirement, symlink and policy denial, SHA-256 and failed checkout.
- [x] Run selected integration tests; observe missing-tool failures.
- [x] Implement fixed argv and strict path/repository verification. Report uncertain outcomes without cleanup.
- [x] Run selected tests to green.

### Task 2: Branch publication

Files: create `tools/git/facades/publish_project_branch.py`; update exports and registrations.
Interfaces: `inspect_project_branch_remote(branch, expected_branch_head)`,
`push_project_branch(branch, expected_branch_head, expected_branch, expected_head, expected_remote_main, expected_remote_feature=null)`,
`create_project_branch_draft_pr(..., title_text, purpose=null, exceptions=null)`.
- [x] Add tests for target worktree execution, unprepared/dirty/moved state, old permission enforcement and unchanged source checkout.
- [x] Run selected tests; implement wrappers around existing remote contracts, then verify.
- [x] Verify no assessment fabrication, no hook bypass and exact-SHA non-force push.

### Task 3: Contract, verification and commit

- [x] Add worktree usage/failure contract and minor release assessment; link the existing owner contracts.
- [x] Run lint, format check, typecheck and full unit/integration profiles.
- [x] Review diff and request independent whole-change review.
- Commit only observed changes, then verify signature and final status using the commit receipt.

## Execution notes

Baseline: branch `feat/isolated-pr-worktrees`, source `b05bac65f7ecfda2956a1d23162ecf3b5aecf87d`; existing Git unit tests: 43 passed.
The current MCP exposes no worktree creation tool, so this bootstrap implementation uses a dedicated branch at the same tree instead of switching to historical code.

## Verification and review

- Full unit profile: 539 passed.
- Full integration profile: 549 passed, 81 skipped; existing dependency deprecation warnings remain.
- Lint and format check passed; Pyright: 0 errors, 0 warnings.
- Independent review findings were reproduced and fixed: ancestor grant mapping, source denied scopes, controller template policy/symlinks, Git pointer backlink binding, and post-create PR base/identity checks.
- Existing public registration order and README tool table are preserved and verified by regression tests.
- Actual private GitHub publication is not claimed by these temporary-repository/mocked-GitHub tests.
