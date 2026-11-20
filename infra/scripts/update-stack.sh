#!/usr/bin/env bash
set -euo pipefail

DB_CTID="${DB_CTID:-110}"
APP_CTID="${APP_CTID:-111}"
LLM_CTID="${LLM_CTID:-112}"
WEB_CTID="${WEB_CTID:-113}"
WEB_HOST="${WEB_HOST:-10.0.0.23}"
WEB_HEALTH_URL="${WEB_HEALTH_URL:-http://$WEB_HOST/health}"

WITH_LLM=0
CHECK_ONLY=0
CURRENT_STAGE=""

log() {
  printf '\n==> %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  if [[ -n "${CURRENT_STAGE}" ]]; then
    printf '\nUpdate failed at stage: %s. See infra/runbooks/rollback.md for per-LXC rollback.\n' "${CURRENT_STAGE}" >&2
  fi
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

ctid_running() {
  local ctid="$1"
  pct status "$ctid" 2>/dev/null | grep -q "status: running" || return 1
}

require_container_command() {
  local ctid="$1"
  local label="$2"
  local command_name="$3"

  if pct exec "$ctid" -- test -x "/usr/local/sbin/${command_name}"; then
    return
  fi

  if ! pct exec "$ctid" -- sh -c "command -v \"${command_name}\" >/dev/null 2>&1"; then
    die "${label} LXC (${ctid}) is missing required command: ${command_name}"
  fi
}

show_help() {
  cat <<EOF
usage: quantfolio-update [OPTIONS]

Update the QuantFolio stack on the Proxmox host.

OPTIONS:
  --with-llm    Include LLM (llama.cpp) update in the stack update
  --check       Run host/container preflight and health checks without updating
  -h, --help    Show this help message
EOF
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-llm)
      WITH_LLM=1
      shift
      ;;
    --check)
      CHECK_ONLY=1
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

trap 'die "Unexpected error"' ERR

if [[ "$(id -u)" -ne 0 ]]; then
  die "Run as root: quantfolio-update"
fi

require_command pct
require_command curl

log "Checking container status"
ctid_running "$DB_CTID" || die "DB LXC ($DB_CTID) is not running"
ctid_running "$APP_CTID" || die "App LXC ($APP_CTID) is not running"
ctid_running "$WEB_CTID" || die "Web LXC ($WEB_CTID) is not running"
if [[ $WITH_LLM -eq 1 ]]; then
  ctid_running "$LLM_CTID" || die "LLM LXC ($LLM_CTID) is not running"
fi

if [[ $CHECK_ONLY -eq 0 ]]; then
  log "Refreshing per-LXC update scripts from repo"
  pct exec "$APP_CTID" -- install -m 0750 /opt/quantfolio/infra/scripts/update-app.sh /usr/local/sbin/quantfolio-update-app
  pct exec "$WEB_CTID" -- install -m 0750 /opt/quantfolio/infra/scripts/update-web.sh /usr/local/sbin/quantfolio-update-web
  if [[ $WITH_LLM -eq 1 ]]; then
    pct exec "$LLM_CTID" -- install -m 0750 /opt/quantfolio/infra/scripts/update-llm.sh /usr/local/sbin/quantfolio-update-llm
  fi
fi

log "Checking per-LXC updater commands"
require_container_command "$APP_CTID" "App" "quantfolio-update-app"
require_container_command "$WEB_CTID" "Web" "quantfolio-update-web"
if [[ $WITH_LLM -eq 1 ]]; then
  require_container_command "$LLM_CTID" "LLM" "quantfolio-update-llm"
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
  CURRENT_STAGE="verify"
  log "Check: final web health at ${WEB_HEALTH_URL}"
  if ! curl -fsS "${WEB_HEALTH_URL}" >/dev/null; then
    die "Web health check failed"
  fi

  CURRENT_STAGE=""
  printf '\nQuantFolio stack check completed successfully. No updates were run.\n'
  exit 0
fi

CURRENT_STAGE="app"
log "Stage: app - updating backend, migrations, and API"
pct exec "$APP_CTID" -- /usr/local/sbin/quantfolio-update-app

CURRENT_STAGE="web"
log "Stage: web - building and deploying frontend"
pct exec "$WEB_CTID" -- /usr/local/sbin/quantfolio-update-web

if [[ $WITH_LLM -eq 1 ]]; then
  CURRENT_STAGE="llm"
  log "Stage: llm - updating LLM service"
  pct exec "$LLM_CTID" -- /usr/local/sbin/quantfolio-update-llm
fi

CURRENT_STAGE="verify"
log "Stage: verify - final health check"
if ! curl -fsS "${WEB_HEALTH_URL}" >/dev/null; then
  die "Web health check failed"
fi

CURRENT_STAGE=""
printf '\nQuantFolio stack update completed successfully.\n'

# Refresh this script from the app LXC repo so subsequent runs pick up any changes
if pct exec "$APP_CTID" -- cat /opt/quantfolio/infra/scripts/update-stack.sh > /tmp/qf-update-stack.new 2>/dev/null; then
  install -m 0750 /tmp/qf-update-stack.new /usr/local/sbin/quantfolio-update
  rm -f /tmp/qf-update-stack.new
  log "Host updater script refreshed"
fi
