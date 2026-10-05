#!/usr/bin/env bash
set -euo pipefail

# Single-node updater: backend + frontend build + Caddy reload, all on ONE Debian host.
# Installed as /usr/local/sbin/quantfolio-update by infra/scripts/install-debian.sh.
# For the multi-LXC Proxmox topology, use the host orchestrator update-stack.sh with the
# per-LXC building blocks update-app.sh / update-web.sh / update-llm.sh instead.

APP_DIR="${APP_DIR:-/opt/quantfolio}"
APP_USER="${APP_USER:-quantfolio}"
BRANCH="${BRANCH:-main}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/quantfolio}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
ENV_FILE="${APP_DIR}/.env"

log() {
  printf '\n==> %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

as_app_user() {
  sudo -H -u "${APP_USER}" "$@"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

read_env_value() {
  local key="$1"
  local value
  value="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "${value}"
}

backup_database() {
  local database_url="$1"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "${BACKUP_DIR}"

  if [[ "${database_url}" == postgresql* ]]; then
    require_command pg_dump
    local pg_url
    pg_url="${database_url/postgresql+psycopg:/postgresql:}"
    local target="${BACKUP_DIR}/quantfolio-${stamp}.dump"
    log "Backing up PostgreSQL database to ${target}"
    pg_dump "${pg_url}" --format=custom --file="${target}"
    echo "${target}"
    return
  fi

  if [[ "${database_url}" == sqlite* ]]; then
    local db_path="${database_url#sqlite:///}"
    if [[ "${db_path}" != /* ]]; then
      db_path="${APP_DIR}/${db_path#./}"
    fi
    [[ -f "${db_path}" ]] || die "SQLite database not found at ${db_path}"
    local target="${BACKUP_DIR}/quantfolio-${stamp}.db.bak"
    log "Backing up SQLite database to ${target}"
    cp "${db_path}" "${target}"
    echo "${target}"
    return
  fi

  die "Unsupported DATABASE_URL scheme for backup: ${database_url}"
}

if [[ "$(id -u)" -ne 0 ]]; then
  die "Run as root: sudo quantfolio-update"
fi

[[ -d "${APP_DIR}/.git" ]] || die "${APP_DIR} is not a git checkout"
[[ -f "${ENV_FILE}" ]] || die "${ENV_FILE} is missing; refusing to update without the existing environment"
id "${APP_USER}" >/dev/null 2>&1 || die "User ${APP_USER} does not exist"
cd "${APP_DIR}"

require_command git
require_command npm
require_command curl
require_command systemctl

DATABASE_URL="$(read_env_value DATABASE_URL)"
[[ -n "${DATABASE_URL}" ]] || die "DATABASE_URL is missing from ${ENV_FILE}"

log "Checking working tree"
if ! as_app_user git -C "${APP_DIR}" diff --quiet || ! as_app_user git -C "${APP_DIR}" diff --cached --quiet; then
  die "Tracked changes exist in ${APP_DIR}; commit or discard them before updating"
fi

BACKUP_PATH="$(backup_database "${DATABASE_URL}")"
log "Backup created at ${BACKUP_PATH}"

log "Fetching and fast-forwarding ${BRANCH}"
as_app_user git -C "${APP_DIR}" fetch origin
as_app_user git -C "${APP_DIR}" checkout "${BRANCH}"
as_app_user git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"

log "Updating backend dependencies"
cd "${APP_DIR}/backend"
as_app_user "${APP_DIR}/backend/.venv/bin/python" -m pip install --disable-pip-version-check -e ".[dkb_robo]"

log "Running database migrations"
as_app_user "${APP_DIR}/backend/.venv/bin/alembic" upgrade head

log "Building frontend"
cd "${APP_DIR}/frontend"
as_app_user npm ci
as_app_user npm run build

log "Restarting services"
systemctl restart quantfolio-api quantfolio-worker
systemctl reload-or-restart caddy

log "Verifying health"
curl --fail --silent --show-error "${HEALTH_URL}"
printf '\nQuantFolio update completed successfully.\n'
