# B3-S GitHub Durable Delivery — dependency and compatibility assessment

- Date: 2026-11-02
- Status: **PostgreSQL 18/Python 3.15 verified; experimental GitHub Receipt Adapter implemented, webhook/worker not connected**
- Scope: GitHub-only PostgreSQL + Procrastinate experiment; Local Task/Shellous unchanged
- Authority: research evidence, not an Accepted ADR or operational contract

## Technology reference

- Procrastinate: [documentation](https://procrastinate.readthedocs.io/en/stable/),
  [external transaction API](https://procrastinate.readthedocs.io/en/stable/howto/production/external_connection.html),
  [schema migrations](https://procrastinate.readthedocs.io/en/stable/howto/production/migrations.html).
- PostgreSQL uses the [Podman Compose 18 instance](../../infra/postgres/README.md)
  on `127.0.0.1:55432`. The official PostgreSQL 18 data path is
  `/var/lib/postgresql/18/docker`.

## Verified environment and transaction experiment

On 2026-11-02, macOS CPython 3.15 executed a local MCP integration probe
against the Podman database. The installed versions match `uv.lock`:

| Package | Locked and imported version |
| --- | --- |
| Procrastinate | 3.10.0 |
| Psycopg | 3.3.6 |
| psycopg-pool | 3.3.3 |

- Python imports passed, and a real authenticated Psycopg connection executed
  `SELECT 1`, `SHOW server_version_num` (major 18) and `SHOW data_directory`.
- A separate temporary proof used a unique, test-owned schema and the installed
  Procrastinate 3.10 schema SQL. It registered a no-op task and called
  `task.configure(connection=conn).defer_async(...)` on the **same**
  Psycopg connection as an event receipt INSERT.
- After COMMIT, exactly one event receipt and one Procrastinate job were visible.
  A second event+job transaction raised an intentional exception and rolled back;
  both counts remained one. The test explicitly dropped only its unique probe schema
  in a `finally` block. The temporary probe tests were removed afterward.
- The packaged schema includes a `CREATE EXTENSION IF NOT EXISTS plpgsql`
  statement in `pg_catalog`; the test first verified the extension already
  existed in that namespace. This does not authorize production schema changes.

**Evidence boundary:** This proves connection compatibility and transaction
coupling in an isolated development experiment, not production receipt/ACK
semantics, concurrent deduplication, crash recovery, GitHub mutation safety or
webhook delivery correctness. It also does not prove all future versions
allowed by the project's open-ended dependency constraints are compatible.

## Current dependency boundary

The user's updated `pyproject.toml` now declares
`procrastinate>=3.10.0` and `psycopg-pool>=3.3.3` in the main
`[project].dependencies`, and the matching `uv.lock` was updated.
Preserve these user changes. However, **main-dependency placement is broader
than the chosen B3-S GitHub-only optional Delivery boundary**. Moving them
to an optional extra or a dedicated dependency group is a separate packaging
decision, not something to infer from successful installation.

## Stage 1 experimental implementation and evidence

This section records the investigation snapshot on 2026-11-02. For current receiver and worker connections, follow the owning contracts linked below.

- The verified [Webhook contract](../contracts/github-webhook-verification.md) produces a source-qualified `VerifiedDelivery`; HTTP receiver wiring is still separate.

- [Internal GitHub Delivery contract](../contracts/github-durable-delivery.md) and
  [receipt adapter](../../src/local_dev_mcp/tools/gh/delivery/receipts.py)
  provide source-qualified Receipt insertion, Procrastinate enqueue, duplicate
  lookup, evidence conflict rejection and fail-closed receipt validation.
- [Live PostgreSQL integration test](../../tests/tools/gh/test_delivery_receipts.py)
  now verifies the **A-option normalized Envelope** (repository ID/name,
  Issue/PR target, action, updated_at and independent envelope schema version)
  stored with a Receipt and Job in one transaction. Conflicting target fields,
  concurrent duplicates, forced post-enqueue DB failure and lost-ACK lookup
  fail closed; Job args contain only event_id, not the original webhook JSON.
- The prior Receipt schema could not reconstruct missing Target fields.
  `MIGRATE_EMPTY_V1_TO_V2` is explicit and **only permits empty legacy
  receipt tables**; populated legacy tables trigger an error and remain intact.
  A new schema is initialized via `RECEIPTS_DDL`. No live service schema
  has been upgraded or connected to a Worker.
- The API requires an **idle autocommit** Psycopg connection so a successful
  return follows a top-level Commit; silently nested savepoints are rejected.
  Duplicate/conflicting requests cannot enqueue a second Job.

## Stage 1 remaining acceptance criteria

1. Review deployment ownership and validate the explicit empty-only migration
   policy against any real GitHub delivery schema before applying it.
2. Wire authenticated webhook admission and commit-aware HTTP ACK handling;
   response/network failure is not proof the database transaction failed.
3. Test commit-unknown connection faults and process crash windows; read-only
   Receipt lookup is available, but no automatic replay decision is implemented.
4. Apply Q65–Q69 Recovery Probe and Operation Contract before allowing any
   Worker to perform or retry external GitHub mutations.
5. Reconcile WIP ADR 0003's SQLite outbox implementation with B3-S rather
   than silently merging that WIP branch or implementing another queue.
6. Keep Worker, Receiver and MCP server lifecycles separate; review
   production database permissions and sensitive output boundaries.

Existing **Q65–Q69** decisions still forbid blind retries after uncertain
external side effects. A Procrastinate job's retry configuration cannot replace
proof that a GitHub mutation is safe to repeat.

Retain `.trail/.gitignore` as local-only and defer `main` integration.
