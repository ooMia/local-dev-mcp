# WSL MCP tool validation — 2026-11-16

Status: partial execution evidence; **not** a certification of all 71 tools.

## Observed runtime

- Connected project: `/home/mia/Github/local-dev-mcp`.
- Controller: `main` at `b48bc73a24bad79c9d1b187e0474f7c07576d53c`.
- Python under pytest: 3.15.2 on Linux. The MCP startup inventory exposes 71 tools.
- Existing tracked user edits in `AGENTS.md`, `tests/tools/git/test_remote_pr.py`, and `uv.lock` were not staged, committed, or reset.
- A separate clean Git-registered Worktrunk checkout was created at `.lab/wt/ce5a02e7f2d306b3d9ae7131` on `test/wsl-mcp-parity-20261116`.

## Verified checks

| Check | Actual outcome | Scope |
| --- | --- | --- |
| `format_check` | PASS, exit 0 | Registered recipe on WSL controller |
| `lint` | PASS, exit 0 | Registered recipe on WSL controller |
| `typecheck` | PASS, exit 0, 0 errors / 0 warnings | Registered recipe on WSL controller |
| Targeted Unit | PASS: 146, deselected 638 | Git, Workspace, Tasks, Release, MCP, Settings, Path Policy, Readiness |
| Scoped Integration | PASS: 620, warnings 5 | `tests/mcp`, `tests/tools/git`, `tests/tools/workspace`, `tests/tools/tasks`, `tests/tools/release`; process execution included |
| Entire Unit collection | FAIL: 27 collection errors | PostgreSQL `libpq` unavailable to Psycopg |
| Wider integration including system tests | FAIL: `test_dag_cli` subprocess collects all tests without a test path | Also blocked by the same missing `libpq` dependency |

## Runtime tool observations

- `create_project_worktrunk`: created a clean linked checkout while the controller remained dirty.
- `inspect_project_worktrunk`, `get_project_worktree_review`, and `inspect_project_branch_remote(backend=worktrunk)`: returned consistent branch, Git head, clean state and remote baseline.
- `get_project_pr_status` on historical merged PR 31: verified merged state and exact-head Project quality success.
- `get_project_index_status`: `unavailable`, `trail_not_configured`.
- `get_project_worktree_index_status`: permission error, `trail` not in `system.allowed_commands`.
- `plan_project_worktree_cleanup`: incomplete, `prune_preview_unavailable`; investigate CLI compatibility before classifying product defect.
- `diagnose_client_elicitation`: Form not declared by current client, so user approval is not testable here.
- `diagnose_documents`: `Blocked`, `RELATIONS_APPROVAL_PENDING`.
- `create_workspace_file` in the ignored linked checkout: safely rejected as `ignored_path`. File-tool policy must not be bypassed to author code inside an ignored worktree.

## Follow-up

1. Provision `libpq` in the WSL environment (Ubuntu usually `libpq5`), then rerun entire Unit, Integration, and nested system test collection. See [Psycopg installation](https://www.psycopg.org/psycopg3/docs/basic/install.html).
2. Re-run this branch's new readiness unit regression in a clean environment; WSL MCP server must be restarted before observing its new code.
3. Distinguish installed binaries from `config.toml` logical grants; only enable Trail by explicit configuration and restart.
4. Inspect the installed Worktrunk CLI's prune subcommand before modifying the adapter.
5. Inventory all 71 tools with actual positive/negative scenarios in isolated workspaces; never treat tool readiness, partial test success, or external-client `Blocked` as full feature acceptance.

The WSL controller's original dirty files were preserved throughout these observations.
