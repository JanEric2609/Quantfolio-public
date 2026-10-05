#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quantfolio}"
APP_USER="${APP_USER:-quantfolio}"
BRANCH="${BRANCH:-main}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/quantfolio}"
ENV_FILE="${APP_DIR}/.env"

log() {
  printf '\n==> %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

as_app_user() {
  runuser -u "${APP_USER}" -- "$@"
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

# The API listens on API_HOST (0.0.0.0 unless narrowed to the LAN address); a narrowed bind
# does not answer on 127.0.0.1, so probe the address it actually listens on.
default_health_url() {
  local host port
  host="$(read_env_value API_HOST)"
  port="$(read_env_value API_PORT)"
  if [[ -z "${host}" || "${host}" == "0.0.0.0" ]]; then
    host="127.0.0.1"
  fi
  printf 'http://%s:%s/health/ready' "${host}" "${port:-8000}"
}

postgres_url() {
  local database_url="$1"

  if [[ "${database_url}" == postgresql+*://* ]]; then
    printf 'postgresql:%s' "${database_url#*:}"
    return
  fi

  printf '%s' "${database_url}"
}

backup_database() {
  local database_url="$1"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "${BACKUP_DIR}"

  if [[ "${database_url}" == postgresql* ]]; then
    require_command pg_dump
    local pg_url
    pg_url="$(postgres_url "${database_url}")"
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

verify_database() {
  local database_url="$1"

  if [[ "${database_url}" == postgresql* ]]; then
    require_command psql
    local pg_url
    pg_url="$(postgres_url "${database_url}")"
    psql "${pg_url}" -v ON_ERROR_STOP=1 -Atqc "SELECT 1" >/dev/null
    return
  fi

  if [[ "${database_url}" == sqlite* ]]; then
    local db_path="${database_url#sqlite:///}"
    if [[ "${db_path}" != /* ]]; then
      db_path="${APP_DIR}/${db_path#./}"
    fi
    [[ -r "${db_path}" ]] || die "SQLite database is not readable at ${db_path}"
    return
  fi

  die "Unsupported DATABASE_URL scheme for verification: ${database_url}"
}

# The units in the repo carry the proxy-header flags (--proxy-headers/--forwarded-allow-ips),
# the bind address and the sandboxing; a unit installed once and never refreshed would keep
# running without them. Same approach as update-llm.sh: copy a changed unit, keep the old one.
UNITS_CHANGED=0
install_unit_if_changed() {
  local name="$1"
  local repo_unit="${APP_DIR}/infra/systemd/${name}"
  local installed="/etc/systemd/system/${name}"
  [[ -f "${repo_unit}" ]] || return 0
  if [[ -f "${installed}" ]] && cmp -s "${repo_unit}" "${installed}"; then
    return 0
  fi
  log "Installing updated systemd unit ${name}"
  [[ -f "${installed}" ]] && cp -p "${installed}" "${installed}.prev"
  install -m 0644 "${repo_unit}" "${installed}"
  UNITS_CHANGED=1
}

if [[ "$(id -u)" -ne 0 ]]; then
  die "Run as root: quantfolio-update-app"
fi

[[ -d "${APP_DIR}/.git" ]] || die "${APP_DIR} is not a git checkout"
[[ -f "${ENV_FILE}" ]] || die "${ENV_FILE} is missing; refusing to update without the existing environment"
id "${APP_USER}" >/dev/null 2>&1 || die "User ${APP_USER} does not exist"

require_command runuser
require_command git
require_command curl

[[ -d "${APP_DIR}" ]] || die "${APP_DIR} does not exist"

HEALTH_URL="${HEALTH_URL:-$(default_health_url)}"

DATABASE_URL="$(read_env_value DATABASE_URL)"
[[ -n "${DATABASE_URL}" ]] || die "DATABASE_URL is missing from ${ENV_FILE}"
if [[ "${DATABASE_URL}" == postgresql* ]]; then
  require_command pg_dump
  require_command psql
fi

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
as_app_user "${APP_DIR}/backend/.venv/bin/python" -m pip install --disable-pip-version-check -e "."

log "Running database migrations"
# DATABASE_URL is NOT inherited here: alembic/env.py resolves it via
# app.foundation.core.config.get_settings(), which falls back to
# backend/.env — a different path than the ${ENV_FILE} this script reads
# (${APP_DIR}/.env, loaded into the real service only via systemd's
# EnvironmentFile=). Without an explicit env var, alembic silently migrates
# a throwaway local SQLite default instead of production Postgres — every
# migration since that mismatch was introduced was a no-op against the real
# database. Pass it through explicitly.
as_app_user env "DATABASE_URL=${DATABASE_URL}" "${APP_DIR}/backend/.venv/bin/alembic" upgrade head

log "Verifying migrations reached head"
MIGRATION_CURRENT="$(as_app_user env "DATABASE_URL=${DATABASE_URL}" "${APP_DIR}/backend/.venv/bin/alembic" current 2>/dev/null | awk '{print $1}')"
MIGRATION_HEAD="$(as_app_user env "DATABASE_URL=${DATABASE_URL}" "${APP_DIR}/backend/.venv/bin/alembic" heads 2>/dev/null | awk '{print $1}')"
[[ -n "${MIGRATION_CURRENT}" && "${MIGRATION_CURRENT}" == "${MIGRATION_HEAD}" ]] \
  || die "Migrations did not reach head (current='${MIGRATION_CURRENT}' head='${MIGRATION_HEAD}') — refusing to restart services against a stale schema"

log "Checking for systemd unit updates"
install_unit_if_changed quantfolio-api.service
install_unit_if_changed quantfolio-worker.service
if [[ "${UNITS_CHANGED}" -eq 1 ]]; then
  systemctl daemon-reload
fi
if [[ -z "$(read_env_value FORWARDED_ALLOW_IPS)" ]]; then
  log "FORWARDED_ALLOW_IPS is not set in ${ENV_FILE}: the unit default (the web LXC, 10.0.0.23) applies. Set it if Caddy runs elsewhere, or every client shares one address for the login lockout."
fi

log "Restarting services"
systemctl restart quantfolio-api quantfolio-worker

log "Verifying database connectivity"
verify_database "${DATABASE_URL}"

log "Verifying API readiness"
attempt=0
max_attempts=10
while [[ $attempt -lt $max_attempts ]]; do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    log "Health check passed"
    printf '\nBackend update completed successfully.\n'
    exit 0
  fi
  attempt=$((attempt + 1))
  if [[ $attempt -lt $max_attempts ]]; then
    sleep 2
  fi
done

die "Health check failed after $max_attempts attempts"
