#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quantfolio}"
APP_USER="${APP_USER:-quantfolio}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-quantfolio-llamacpp}"
LLAMA_ENV="${LLAMA_ENV:-/etc/quantfolio/llama.env}"
REPO_UNIT="${APP_DIR}/infra/systemd/quantfolio-llamacpp.service"
INSTALLED_UNIT="/etc/systemd/system/quantfolio-llamacpp.service"

# llama-server listens on LLAMA_BIND_HOST (0.0.0.0 unless narrowed to the LAN address), and a
# narrowed bind does not answer on 127.0.0.1. /health needs no API key.
default_health_url() {
  local host=""
  if [[ -r "${LLAMA_ENV}" ]]; then
    host="$(grep -E '^LLAMA_BIND_HOST=' "${LLAMA_ENV}" | tail -n 1 | cut -d= -f2- | tr -d "\"'" || true)"
  fi
  if [[ -z "${host}" || "${host}" == "0.0.0.0" ]]; then
    host="127.0.0.1"
  fi
  printf 'http://%s:8080/health' "${host}"
}
HEALTH_URL="${HEALTH_URL:-$(default_health_url)}"

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

if [[ "$(id -u)" -ne 0 ]]; then
  die "Run as root: quantfolio-update-llm"
fi

[[ -d "${APP_DIR}/.git" ]] || die "${APP_DIR} is not a git checkout"
id "${APP_USER}" >/dev/null 2>&1 || die "User ${APP_USER} does not exist"

require_command runuser
require_command git
require_command curl

log "Checking working tree"
if ! as_app_user git -C "${APP_DIR}" diff --quiet || ! as_app_user git -C "${APP_DIR}" diff --cached --quiet; then
  die "Tracked changes exist in ${APP_DIR}; commit or discard them before updating"
fi

log "Fetching and fast-forwarding ${BRANCH}"
as_app_user git -C "${APP_DIR}" fetch origin
as_app_user git -C "${APP_DIR}" checkout "${BRANCH}"
as_app_user git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"

log "Checking for systemd unit updates"
if [[ -f "${REPO_UNIT}" ]]; then
  if cmp -s "${REPO_UNIT}" "${INSTALLED_UNIT}"; then
    log "Systemd unit already up to date"
  else
    log "Installing updated systemd unit"
    cp "${REPO_UNIT}" "${INSTALLED_UNIT}"
    systemctl daemon-reload
  fi
else
  log "Systemd unit not found in repository; skipping update"
fi

log "Restarting ${SERVICE} (model load may take a while)"
systemctl restart "${SERVICE}"

log "Verifying health"
attempt=0
max_attempts=60
while [[ $attempt -lt $max_attempts ]]; do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    log "Health check passed"
    printf '\nLLM update completed successfully.\n'
    exit 0
  fi
  attempt=$((attempt + 1))
  if [[ $attempt -lt $max_attempts ]]; then
    sleep 2
  fi
done

die "Health check failed after $max_attempts attempts"
