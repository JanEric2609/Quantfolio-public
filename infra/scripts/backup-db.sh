#!/usr/bin/env bash
# Nightly database backup on the app LXC (111), run by /etc/cron.d/quantfolio-backup.
#
# Writes a pg_dump custom-format file to BACKUP_DIR, deletes dumps older than
# RETENTION_DAYS (the pre-update dumps update-app.sh writes share the name
# pattern and are pruned the same way), and records the result in
# STATUS_FILE, which the daily self-check reads. Restore with restore-db.sh.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quantfolio}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/quantfolio}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
STATUS_FILE="${STATUS_FILE:-/var/lib/quantfolio-backup/status.json}"

write_status() {
  # Readable by the app user: the self-check runs as quantfolio, the dumps stay root-only.
  local status="$1" detail="$2" file="${3:-}" bytes="${4:-0}"
  install -d -m 0755 "$(dirname "${STATUS_FILE}")"
  local tmp="${STATUS_FILE}.tmp"
  # JSON string escaping: backslash first, then quotes; newlines and tabs become spaces.
  detail="${detail//\\/\\\\}"; detail="${detail//\"/\\\"}"; detail="${detail//$'\n'/ }"; detail="${detail//$'\t'/ }"
  printf '{"status": "%s", "finished_at": "%s", "file": "%s", "bytes": %s, "detail": "%s"}\n' \
    "${status}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${file}" "${bytes}" "${detail}" >"${tmp}"
  chmod 0644 "${tmp}"
  mv -f "${tmp}" "${STATUS_FILE}"
}

trap 'write_status failed "backup-db.sh exited with an error (see /var/log/quantfolio-backup.log)"' ERR

if [[ -z "${DATABASE_URL:-}" && -f "${ENV_FILE}" ]]; then
  DATABASE_URL="$(grep -E '^DATABASE_URL=' "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
  DATABASE_URL="${DATABASE_URL%\"}"; DATABASE_URL="${DATABASE_URL#\"}"
  DATABASE_URL="${DATABASE_URL%\'}"; DATABASE_URL="${DATABASE_URL#\'}"
fi
[[ -n "${DATABASE_URL:-}" ]] || { write_status failed "DATABASE_URL is not set and not in ${ENV_FILE}"; exit 1; }

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
# A dump that pg_restore cannot list is no backup.
pg_restore --list "${TARGET}" >/dev/null
find "${BACKUP_DIR}" -type f -name "quantfolio-*.dump" -mtime "+${RETENTION_DAYS}" -delete
write_status ok "pg_dump and pg_restore --list succeeded" "${TARGET}" "$(stat -c %s "${TARGET}")"
echo "${TARGET}"
