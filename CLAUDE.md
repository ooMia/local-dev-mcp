# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`local-dev-mcp` is an MCP server that lets a ChatGPT session control a local workspace: file read/search/edit/delete, lint/format/test execution, and local Git operations. It does not provide arbitrary command execution or OS isolation. GitHub remote queries, feature branch publishing, Draft PR creation, and explicit PR merge require authenticated `gh` and separate permissions.

## Setup and execution

Python 3.15+ and `uv` are required. Git tools additionally require Git installed.

```sh
uv sync --locked                      # prepare dev environment
uv run --locked local-dev-mcp --workspace /path/to/workspace run   # stdio MCP server
uv run --locked local-dev-mcp --workspace /path/to/workspace connect  # startup precheck + Tunnel client
```

`--workspace` goes before `run`/`connect`; omitted it defaults to the current directory. To also allow MCP editing of root `config.toml` and `.gitignore`, add `--allow-policy-edits`.

Environment values live in `.env` (copied from `.env.example`; never commit real values). After changing settings, restart the server.

MCP tools offered (read from the server's input schema before calling):

- Workspace: `list_workspace_entries`, `find_workspace_paths`, `search_workspace_text`, `read_workspace_file`, `replace_workspace_file`, `create_workspace_file`, `delete_workspace_file`
- Git: `get_project_head`, `get_project_status`, `get_project_diff`, `stage_project_paths`, `unstage_project_paths`, `commit_project_changes`, `create_project_branch`, `switch_project_branch`, `merge_project_branch`, `inspect_project_policy`, `compare_project_refs`, `inspect_project_commit_history`, plus worktree/PR/remote variants
- Tasks: `list_project_tasks`, `start_project_task`, `get_project_task`, `cancel_project_task`
- Release: `get_release_plan`, `prepare_project_release`
- Readiness/index/others: `get_tool_readiness`, `get_project_index_status`, `refresh_project_index`, `diagnose_documents`, `diagnose_client_elicitation`, `probe_client_elicitation`, `probe_client_elicitation_mrtr`

File edits: read the needed range with `read_workspace_file`, then `replace_workspace_file` with the same `read_ref` and new `replacement`. On `stale_version`, re-read and rebuild the edit. Create/delete use `request_id` for lost-response recovery; replayed receipts are historical outcomes.

## Commands

```sh
uv run --locked pyright                                # type check (strict)
uv run --locked ruff check src tests conftest.py       # lint
uv run --locked ruff format --check src tests conftest.py  # format check
uv run --locked pytest                                 # default regression (unit + integration, excludes packaging/e2e)
uv run --locked pytest -n 0 -m unit                    # unit, no OS resources
uv run --locked pytest -n 4 -m 'integration and not packaging'  # real resources/protocol
uv run --locked pytest -n 0                            # serial reproduction
uv run --locked pytest -n 0 --durations=15             # bottlenecks
uv run --locked pytest -n 0 -m packaging               # install verification
uv run --locked python -m local_dev_mcp.tools.release check --base <SHA> --head <SHA>
uv run --locked python -m local_dev_mcp.tools.release plan
```

Run a single test by node ID: `uv run --locked pytest tests/tools/workspace/test_editing.py::test_name -n 0`.

Small development task flow: new source/test → confirm selected test fails → implement → format/lint/checks → review diff. After format, re-read the file for the new `read_ref`. Default completion is verified working-tree changes plus a reviewable diff; commit is a separate request. Full stage uses `paths=["."]`; commit does not auto-stage.

## Architecture

Entry: `local_dev_mcp.app.cli:main` parses `--workspace`, `--allow-policy-edits`, and the `run`/`connect` subcommand, loads the workspace `Policy`, then dispatches.

- `run`: replaces environment with a minimal set, disables FastMCP auto-dotenv (`FASTMCP_ENV_FILE=/dev/null`), imports and runs `mcp.server.create_server(policy)` over stdio.
- `connect`: runs startup precheck then the Tunnel client, which executes `run` in the same Python environment.

Server assembly (`app/runtime.py` → `mcp/server.py`): `ServerSettings.load(policy)` builds the startup snapshot (policy, git/commit preparation issues, environment, redactor, task/index/workspace managers). `create_server` registers every `ToolDefinition` in `mcp/server.py::TOOLS` once via `mcp/registry.py::register_tools`, with `SafeToolDiagnostics` and `ToolAuthorization` middleware.

Invocation order: authorization middleware → static readiness → strict input validation → Depends preparation → tool body. Each tool has an immutable `ToolDefinition` (snake_case name, same filename, `GetXxxTool` PascalCase runtime class) registered explicitly in the manual `TOOLS` list — no auto-inference from files/exports. Duplicate names are rejected before any runtime is created.

Tool modules live under `src/local_dev_mcp/tools/<domain>/` (workspace, git, tasks, release, indexing, gh/…, client_elicitation, documents). `app/` owns startup assembly, `mcp/` owns server assembly/registration/calling interface, root `settings`/`readiness`/`errors` hold shared value contracts.

Git follows facade → command function → execution support layering; command functions own fixed argv and Git output parsing, facades assemble work order and public results.

## Conventions

Commit title format: `type(scope): 변화` (e.g. `feat(git): …`, `fix(workspace): …`, `refactor(lint): …`, `docs(…)`). Non-breaking intent may use `!`. Branches: `feat/<topic>`, `fix/<topic>`, `refactor/<topic>`, `docs/<topic>`, `test/<topic>`, `build/<topic>`, `ci/<topic>`, `chore/<topic>` from current `main`; `release/X.Y.Z` only for version prep. No standing `develop` or major/minor/patch shared branches.

Each commit touches the contract/source/test/doc that actually change; before integration rebase onto current main and squash verified work units into one message. Add an assessment file per change; `none` still needs a rationale. Never edit/delete existing records; add explicit correction files.

Pyright strict on `src/` and `tests/`; Ruff lint and format. Tool changes must verify schema, normal behavior, bad arguments, permission denial, per-server state isolation, and safe errors. Package/entry-point changes must verify import and install boundary.

Test layout: `tests/app/` (CLI/startup/import), `tests/mcp/` (registration/schema/boundary), `tests/test_<module>.py` (root modules), `tests/tools/<tool>/` (tool behavior), `tests/system/` (real stdio/full flows/install boundary), `tests/e2e/` (real ChatGPT acceptance), `tests/support/` (shared builders/fixtures/DAG). Name files `test_<target>.py`, functions `test_<action>_<condition>`. Markers: `unit`, `integration`, `e2e`, `packaging` — exactly one per item, enforced at collection.

`.env` keys from `.env.example`: `CONTROL_PLANE_TUNNEL_ID`, `CONTROL_PLANE_API_KEY`, `GH_TOKEN`, `GIT_USER_NAME`, `GIT_USER_EMAIL`, `GIT_COMMIT_SIGN`, `GIT_GPG_HOME`, `GIT_SIGNING_KEY`, `GIT_GPG_PROGRAM`, `TRAIL_EXECUTABLE`, `TRAIL_REFRESH_ON_WRITE`. Never paste real token/key values into Chat, logs, or CLI arguments.
