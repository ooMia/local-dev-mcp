# B3-S development PostgreSQL (Podman)

This Compose stack is a **local development/test dependency**, not the
LOCAL_DEV_MCP server, GitHub webhook receiver, or Procrastinate worker.
Those components are not started here. Current Stage 1 implementation and
package import compatibility remain unverified.

## Prepare

On macOS, install Podman and a Compose provider, initialize a Podman
machine if absent, and start it when needed:

```sh
podman machine init    # only if no machine exists
podman machine start   # only if stopped
podman compose --help  # confirm an external Compose provider is available
```

`podman compose` delegates to an external provider (such as
`podman-compose` or `docker-compose`); installing only Podman does not
necessarily install a provider.

From the repository root:

```sh
cd infra/postgres
cp .env.example .env
openssl rand -hex 24    # use the output as B3S_DB_PASSWORD in .env; never commit it
```

Edit **this directory's** `.env` with a nonempty password. It is excluded
by the repository's existing root `.gitignore`. Do not put it in
`.env.example`, in chat, or in the root MCP `.env`. Compose environment
interpolation is not the MCP server's configuration-loading contract.

## Reset PostgreSQL 17 → 18 (destructive; local test data only)

The previous Compose guide ran from `infra/postgres`, giving the project
the default name `postgres`. The reset script targets that exact Compose
project and the single declared `postgres_data` volume. **It permanently
deletes the previous PostgreSQL 17 test database.** This is initialization,
not migration. If your 17 database contains anything worth keeping, back it
up first and do not run the reset.

Run from the repository root after preparing `infra/postgres/.env`:

```sh
bash infra/postgres/reset-to-18.sh --discard-data
```

The script verifies the local prerequisites, pulls PostgreSQL 18 **before**
deleting data, then performs the equivalent of:

```sh
cd infra/postgres
podman compose -p postgres -f compose.yaml down --volumes
podman compose -p postgres -f compose.yaml up -d
```

`down --volumes` deletes the project's named volume, not just its
container; it also removes the project's attached anonymous volumes.
Do not run this reset if the old service was started using another
Compose project name, a different provider/connection, or if the
`postgres` project contains other services or important volumes.
Inspect the original project first in that case.

The script checks readiness, authenticates by TCP **inside** the container,
then verifies PostgreSQL major version 18 and
`data_directory = /var/lib/postgresql/18/docker`.

## Start, inspect, and stop after initialization

Run from `infra/postgres` so Compose reads the intended `.env`:

```sh
podman compose -p postgres -f compose.yaml up -d
podman compose -p postgres -f compose.yaml ps
podman compose -p postgres -f compose.yaml exec -T postgres pg_isready -U b3s_dev -d b3s_dev
podman compose -p postgres -f compose.yaml exec -T postgres psql -U b3s_dev -d b3s_dev -c 'SHOW server_version;'
podman compose -p postgres -f compose.yaml down
```

The container uses the PostgreSQL 18 official image. Host connections
target `127.0.0.1:55432` by default (`B3S_DB_PORT` may override the
host port). Inside this Compose network, the service is `postgres:5432`.
The database and initial superuser are both `b3s_dev`. This is a
single-user local test setup, **not a production security profile**.

The container's `pg_isready` health check and local-socket `psql` test
prove server readiness, **not host TCP password authentication**. For
that, use a host PostgreSQL client if installed:

```sh
psql -h 127.0.0.1 -p 55432 -U b3s_dev -d b3s_dev -W -c 'SELECT 1'
```

If `B3S_DB_PORT` was changed, substitute the configured host port.
Applications do not automatically inherit Compose `.env`; give them
separately scoped database credentials.

The named `postgres_data` volume survives `podman compose down`.
PostgreSQL 18 uses `/var/lib/postgresql/18/docker` as its data directory
and mounts the persistent volume at `/var/lib/postgresql`. Retaining the
old PostgreSQL 17 data volume does **not** migrate the database.
Changing `B3S_DB_PASSWORD` later **does not reset an initialized DB
password**: PostgreSQL image initialization variables apply only to an
empty data directory. Use `reset-to-18.sh --discard-data` only when
permanent deletion of the project's existing database is intended.

Do not print or share `podman compose config` output; expanded
configuration can contain the database password. Use a dedicated
development password, not a real GitHub or Tunnel credential.

## Stage 1 boundary

The Podman stack supplies PostgreSQL only. It **does not** install
`procrastinate==3.10.0`, regenerate `uv.lock`, run Python 3.15 imports,
install queue tables, accept webhooks, or validate event + job transactions.
See the [B3-S compatibility assessment](../../docs/research/b3s-delivery-compatibility-2026-11-02.md).
The approved B3-S GitHub adapter remains optional; local task execution
retains its Shellous contract.

References: [Podman Compose](https://docs.podman.io/en/v5.6.2/markdown/podman-compose.1.html),
[official PostgreSQL image](https://hub.docker.com/_/postgres).
