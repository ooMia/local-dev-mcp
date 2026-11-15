# M4 Windows-native process lock acceptance evidence (2026-11-15)

## Verified run

- Repository: `ooMia/local-dev-mcp`
- Source PR: [#30](https://github.com/ooMia/local-dev-mcp/pull/30)
- Runner: GitHub-hosted `windows-2025` (native Windows, not WSL)
- Runtime: CPython **3.15.2**, installed with uv; `uv sync --locked` succeeded
- Job: [Windows M4 native](https://github.com/ooMia/local-dev-mcp/actions/runs/43680981385/job/132711324772)
- Selected integration tests: `tests/mcp/test_trail_m4_interprocess_lock.py`, `tests/mcp/test_trail_m4_failure_recovery.py`, `tests/mcp/test_trail_m4_public_worktree_index.py`
- Observed pytest result: **9 passed, 1 skipped, 1 warning in 38.92s**; process exit was successful

## Interpreted coverage

The targeted tests use disposable Git repositories and registered linked worktrees. They exercise the cross-process `msvcrt.locking` coordination path, same-root serialization, separate roots, lock timeout and cancellation, termination recovery, Git HEAD/Clean retention and failure-path behavior. The workflow checks `os.name == 'nt'` and `sys.platform == 'win32'` before running the tests.

The single skipped case is `test_m4_public_worktree_status_refresh_and_cross_branch_isolation`, which intentionally requires the real `trail` executable. The Windows GitHub runner does not install Trail Code. Thus this record establishes **native Windows MCP process-lock and recovery compatibility**, not actual Windows Trail CLI graph creation, skeleton/callers query routing, ACL edge cases, case-insensitive path collisions, or non-cooperating external Trail concurrency.

## Follow-up gates

- Install and verify a real Windows `trail.exe`; run the previously skipped isolated-worktree graph test and explicit-root `skeleton`/`callers` tests.
- Validate Windows path case/ACL and executable lookup requirements under realistic runtime deployment.
- Keep the path-filtered CI workflow bounded to 10 minutes; hosted private-repository usage consumes included GitHub Actions compute and may incur charges when exhausted.

The macOS working tree and user-created changes were not modified by this remote-branch validation.
