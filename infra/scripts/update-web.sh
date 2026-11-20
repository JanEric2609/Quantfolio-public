#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quantfolio}"
FRONTEND_DIR="${FRONTEND_DIR:-$APP_DIR/frontend}"
APP_USER="${APP_USER:-quantfolio}"
BRANCH="${BRANCH:-main}"
APP_UPSTREAM="${APP_UPSTREAM:-10.0.0.21:8000}"
HEALTH_URL="${HEALTH_URL:-http://${APP_UPSTREAM}/health}"
WEB_PROBE_URL="${WEB_PROBE_URL:-}"
NODE_MAX_OLD_SPACE="${NODE_MAX_OLD_SPACE:-1536}"

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

require_node_18() {
  local major
  major="$(node -p "process.versions.node.split('.')[0]")"
  [[ "${major}" =~ ^[0-9]+$ ]] || die "Unable to determine Node.js version"
  [[ "${major}" -ge 18 ]] || die "Node.js >= 18 is required; found $(node --version)"
}

if [[ "$(id -u)" -ne 0 ]]; then
  die "Run as root: quantfolio-update-web"
fi

[[ -d "${APP_DIR}/.git" ]] || die "${APP_DIR} is not a git checkout"
[[ -d "${FRONTEND_DIR}" ]] || die "${FRONTEND_DIR} does not exist"
[[ -f "${FRONTEND_DIR}/package-lock.json" ]] || die "${FRONTEND_DIR}/package-lock.json is required for npm ci"
id "${APP_USER}" >/dev/null 2>&1 || die "User ${APP_USER} does not exist"

require_command runuser
require_command git
require_command node
require_command npm
require_command curl
require_command caddy
require_node_18

log "Checking working tree"
if ! as_app_user git -C "${APP_DIR}" diff --quiet || ! as_app_user git -C "${APP_DIR}" diff --cached --quiet; then
  die "Tracked changes exist in ${APP_DIR}; commit or discard them before updating"
fi

log "Fetching and fast-forwarding ${BRANCH}"
as_app_user git -C "${APP_DIR}" fetch origin
as_app_user git -C "${APP_DIR}" checkout "${BRANCH}"
as_app_user git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"

log "Installing frontend dependencies"
as_app_user npm --prefix "${FRONTEND_DIR}" ci

log "Building frontend"
as_app_user env NODE_OPTIONS="--max-old-space-size=${NODE_MAX_OLD_SPACE}" npm --prefix "${FRONTEND_DIR}" run build

log "Validating Caddy config"
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

log "Reloading Caddy"
systemctl reload-or-restart caddy

log "Verifying health"
attempt=0
max_attempts=10
while [[ $attempt -lt $max_attempts ]]; do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    # In TLS/hostname deployments, localhost HTTP may only return an HTTPS
    # redirect. Validate the built SPA artifact locally unless an explicit
    # public web probe URL is provided.
    if [[ -n "${WEB_PROBE_URL}" ]]; then
      if curl -fsSLk "${WEB_PROBE_URL}" | grep -q 'id="root"'; then
        log "Health check passed"
        printf '\nFrontend update completed successfully.\n'
        exit 0
      fi
    elif grep -q 'id="root"' "${FRONTEND_DIR}/dist/index.html"; then
      log "Health check passed"
      printf '\nFrontend update completed successfully.\n'
      exit 0
    fi
  fi
  attempt=$((attempt + 1))
  if [[ $attempt -lt $max_attempts ]]; then
    sleep 2
  fi
done

die "Health check failed after $max_attempts attempts"
