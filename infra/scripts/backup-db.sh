#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/quantfolio}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DATABASE_URL="${DATABASE_URL:?DATABASE_URL must be set}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/quantfolio-${STAMP}.dump"

# Strip SQLAlchemy driver suffix so pg_dump gets a standard libpq URL
postgres_url() {
  local url="$1"
  if [[ "${url}" == postgresql+*://* ]]; then
    printf 'postgresql:%s' "${url#*:}"
  else
    printf '%s' "${url}"
  fi
}

PG_URL="$(postgres_url "${DATABASE_URL}")"
# Dumps hold every secret-bearing table: owner-only, like the .env they sit beside.
umask 077
mkdir -p "${BACKUP_DIR}"
pg_dump "${PG_URL}" --format=custom --file="${TARGET}"
find "${BACKUP_DIR}" -type f -name "quantfolio-*.dump" -mtime "+${RETENTION_DAYS}" -delete
echo "${TARGET}"
