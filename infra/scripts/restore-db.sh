#!/usr/bin/env bash
# Restore a backup-db.sh dump into a database on the DB LXC (110).
#
# usage: restore-db.sh <dump file> [target database]
#
# The target defaults to quantfolio_restored, a scratch copy to check a dump
# against. Restoring over the live database (target "quantfolio") needs
# --replace-live and stopped services; see infra/runbooks/backup-restore.md.
set -euo pipefail

usage() { echo "usage: $0 <dump file> [target database] [--replace-live]" >&2; exit 2; }

DUMP="${1:-}"; [[ -n "${DUMP}" ]] || usage
TARGET_DB="${2:-quantfolio_restored}"
REPLACE_LIVE=0
[[ "${3:-}" == "--replace-live" ]] && REPLACE_LIVE=1

APP_DIR="${APP_DIR:-/opt/quantfolio}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"

[[ -f "${DUMP}" ]] || { echo "ERROR: dump not found: ${DUMP}" >&2; exit 1; }
pg_restore --list "${DUMP}" >/dev/null || { echo "ERROR: ${DUMP} is not a readable pg_dump archive" >&2; exit 1; }

if [[ -z "${DATABASE_URL:-}" && -f "${ENV_FILE}" ]]; then
  DATABASE_URL="$(grep -E '^DATABASE_URL=' "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
  DATABASE_URL="${DATABASE_URL%\"}"; DATABASE_URL="${DATABASE_URL#\"}"
fi
[[ -n "${DATABASE_URL:-}" ]] || { echo "ERROR: DATABASE_URL is not set" >&2; exit 1; }
URL="${DATABASE_URL}"
[[ "${URL}" == postgresql+*://* ]] && URL="postgresql:${URL#*:}"
LIVE_DB="${URL##*/}"; LIVE_DB="${LIVE_DB%%\?*}"
BASE_URL="${URL%/*}"

if [[ "${TARGET_DB}" == "${LIVE_DB}" && "${REPLACE_LIVE}" -ne 1 ]]; then
  echo "ERROR: ${TARGET_DB} is the live database. Stop quantfolio-api and quantfolio-worker first," >&2
  echo "       then rerun with --replace-live (see infra/runbooks/backup-restore.md)." >&2
  exit 1
fi

echo "Restoring ${DUMP} into ${TARGET_DB}"
# --clean --if-exists drops what the dump recreates; --create is not used, so the
# target database must exist (create it on the DB LXC first for a scratch copy).
pg_restore --dbname="${BASE_URL}/${TARGET_DB}" --clean --if-exists --no-owner --exit-on-error "${DUMP}"
psql "${BASE_URL}/${TARGET_DB}" -v ON_ERROR_STOP=1 -Atc \
  "SELECT 'alembic ' || version_num FROM alembic_version UNION ALL SELECT 'users ' || count(*) FROM users"
echo "Restore finished."
