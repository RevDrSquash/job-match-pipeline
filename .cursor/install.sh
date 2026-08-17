#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for the Job Match Pipeline.
#
#   1. Install Postgres 18 + pgvector (matches docker-compose pgvector/pgvector:pg18).
#   2. Create a project virtualenv and install the package with dev extras.
#   3. Start Postgres and apply Alembic migrations (CREATE EXTENSION vector + schema).
#
# Safe to run repeatedly: apt installs are no-ops when present, pip is cached,
# and `alembic upgrade head` only applies missing revisions.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

PG_VERSION="${PG_VERSION:-18}"
VENV_DIR="${REPO_DIR}/.venv"

log() { echo "[install] $*"; }

log "installing system packages (Postgres ${PG_VERSION} + pgvector)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  curl ca-certificates gnupg lsb-release python3-venv >/dev/null

if ! dpkg -s "postgresql-${PG_VERSION}" >/dev/null 2>&1; then
  log "adding PostgreSQL APT (PGDG) repository"
  sudo install -d /usr/share/postgresql-common/pgdg
  sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends \
    "postgresql-${PG_VERSION}" "postgresql-${PG_VERSION}-pgvector" >/dev/null
fi

log "creating virtualenv and installing dependencies"
if [ ! -x "${VENV_DIR}/bin/python" ]; then
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null
"${VENV_DIR}/bin/python" -m pip install -e '.[dev]'

log "starting Postgres and applying migrations"
bash "${REPO_DIR}/.cursor/start-postgres.sh"
"${VENV_DIR}/bin/alembic" upgrade head

log "done"
