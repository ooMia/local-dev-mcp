#!/usr/bin/env bash
# Destroy ONLY the previously documented local B3-S "postgres" Compose project's
# containers and volumes, then initialize PostgreSQL 18. Not a data migration.
set -euo pipefail

if [[ "$#" -ne 1 || "$1" != "--discard-data" ]]; then
  printf 'Usage: bash infra/postgres/reset-to-18.sh --discard-data\n' >&2
  printf 'Deletes the PostgreSQL 17 development database and named volume.\n' >&2
  exit 2
fi

cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -f .env ]]; then
  printf 'Missing infra/postgres/.env; prepare it from .env.example before reset.\n' >&2
  exit 1
fi
if ! grep -Eq '^B3S_DB_PASSWORD=.+$' .env; then
  printf 'B3S_DB_PASSWORD must be set in infra/postgres/.env.\n' >&2
  exit 1
fi
if ! command -v podman >/dev/null 2>&1 || ! podman info >/dev/null 2>&1; then
  printf 'Podman is unavailable or its machine is stopped.\n' >&2
  exit 1
fi
if ! podman compose --help >/dev/null 2>&1; then
  printf 'Podman Compose provider is unavailable.\n' >&2
  exit 1
fi

# The original instructions ran Compose from infra/postgres (default project
# name "postgres"). Pin both project and file, so an ambient project name or
# COMPOSE_FILE cannot redirect the destructive down operation.
compose=(podman compose -p postgres -f compose.yaml)

printf 'Pulling PostgreSQL 18 before touching the existing PostgreSQL 17 data...\n'
"${compose[@]}" pull postgres

printf 'Removing ONLY the "postgres" Compose project containers and volumes...\n'
"${compose[@]}" down --volumes

printf 'Initializing PostgreSQL 18 with a fresh data volume...\n'
"${compose[@]}" up -d

ready=false
for ((attempt = 1; attempt <= 30; attempt++)); do
  if "${compose[@]}" exec -T postgres pg_isready -U b3s_dev -d b3s_dev >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 2
done

if [[ "$ready" != true ]]; then
  printf 'PostgreSQL readiness failed. Inspect: podman compose -p postgres -f compose.yaml logs postgres\n' >&2
  exit 1
fi

# TCP authentication within the container; not a host published-port test.
sql() {
  "${compose[@]}" exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U b3s_dev -d b3s_dev -Atqc "$1"' \
    sh "$1" | tr -d '\r'
}

server_version="$(sql 'SHOW server_version_num;')"
data_directory="$(sql 'SHOW data_directory;')"
if [[ "$server_version" != 18* ]]; then
  printf 'Unexpected PostgreSQL version: %s\n' "$server_version" >&2
  exit 1
fi
if [[ "$data_directory" != "/var/lib/postgresql/18/docker" ]]; then
  printf 'Unexpected PostgreSQL data directory: %s\n' "$data_directory" >&2
  exit 1
fi

printf 'PostgreSQL 18 initialized; version_num=%s, data_directory=%s\n' "$server_version" "$data_directory"
printf 'Run "podman compose -p postgres -f infra/postgres/compose.yaml ps" to inspect status.\n'
