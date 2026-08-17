#!/usr/bin/env bash
# Bring up the local Postgres 18 (pgvector) cluster the app expects.
#
# Idempotent and safe to run on every boot: it starts the cluster if it is not
# already accepting connections, then ensures the `postgres` role password and
# the `jobmatch` database exist. Schema migrations live in install (alembic).
set -euo pipefail

PG_VERSION="${PG_VERSION:-18}"
PG_CLUSTER="${PG_CLUSTER:-main}"
PG_PORT="${PG_PORT:-5432}"
PG_HOST="127.0.0.1"
DB_NAME="${DB_NAME:-jobmatch}"
DB_PASSWORD="${DB_PASSWORD:-postgres}"

log() { echo "[start-postgres] $*"; }

# Create the cluster if the package post-install did not (e.g. fresh data dir).
if [ ! -d "/etc/postgresql/${PG_VERSION}/${PG_CLUSTER}" ]; then
  log "creating cluster ${PG_VERSION}/${PG_CLUSTER}"
  sudo pg_createcluster "${PG_VERSION}" "${PG_CLUSTER}" -- --auth-host=scram-sha-256
fi

if ! pg_isready -h "${PG_HOST}" -p "${PG_PORT}" -q; then
  log "starting cluster ${PG_VERSION}/${PG_CLUSTER}"
  sudo pg_ctlcluster "${PG_VERSION}" "${PG_CLUSTER}" start
fi

# Wait until the server accepts connections.
for _ in $(seq 1 30); do
  if pg_isready -h "${PG_HOST}" -p "${PG_PORT}" -q; then
    break
  fi
  sleep 1
done
pg_isready -h "${PG_HOST}" -p "${PG_PORT}"

# Ensure the postgres role has the password the app connects with.
sudo -u postgres psql -v ON_ERROR_STOP=1 -c \
  "ALTER USER postgres PASSWORD '${DB_PASSWORD}';" >/dev/null

# Ensure the application database exists.
if ! sudo -u postgres psql -tAc \
  "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}';" | grep -q 1; then
  log "creating database ${DB_NAME}"
  sudo -u postgres createdb -O postgres "${DB_NAME}"
fi

log "ready on ${PG_HOST}:${PG_PORT} (db=${DB_NAME})"
