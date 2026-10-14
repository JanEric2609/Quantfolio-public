#!/usr/bin/env bash
# Browser smoke test: a fresh SQLite database with one admin, the API on
# :8000, the production build on :4173 (vite preview proxies /api), then
# Playwright opens every page. Run from the repo root or frontend/.
#
#   bash frontend/e2e/run-smoke.sh            # all pages, desktop and phone
#   bash frontend/e2e/run-smoke.sh -g /plan   # extra args go to playwright
#
# CI runs it inside a network namespace with only loopback, so no page can
# reach a provider; locally it uses whatever network there is.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
export SMOKE_USER="${SMOKE_USER:-smoke}"
export SMOKE_PASSWORD="${SMOKE_PASSWORD:-smoke-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')}"
export DATABASE_URL="sqlite:///${WORK}/smoke.db"
API_LOG="${WORK}/api.log"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  if [[ "${KEEP_SMOKE_LOGS:-0}" == "1" || "${status:-0}" -ne 0 ]]; then
    echo "API log: ${API_LOG}"
    tail -n 40 "${API_LOG}" 2>/dev/null || true
  else
    rm -rf "${WORK}"
  fi
}
trap 'status=$?; cleanup' EXIT

wait_for() {
  local url="$1" name="$2"
  for _ in $(seq 1 90); do
    curl -fsS -o /dev/null "$url" 2>/dev/null && return 0
    sleep 1
  done
  echo "ERROR: ${name} did not come up at ${url}" >&2
  return 1
}

cd "${ROOT}/backend"
.venv/bin/alembic upgrade head >/dev/null 2>&1
.venv/bin/python scripts/seed_smoke_db.py
# APP_ENV=local: an http session cookie. FRONTEND_ORIGIN: the preview server,
# which the cross-site guard must accept.
APP_ENV=local FRONTEND_ORIGIN="http://localhost:4173" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 >"${API_LOG}" 2>&1 &
PIDS+=($!)
wait_for "http://127.0.0.1:8000/health" "API"

cd "${ROOT}/frontend"
[[ -d dist && "${SMOKE_SKIP_BUILD:-0}" == "1" ]] || npm run build >/dev/null
npx vite preview --host localhost --port 4173 --strictPort >"${WORK}/preview.log" 2>&1 &
PIDS+=($!)
wait_for "http://localhost:4173/" "frontend preview"

npx playwright test "$@"
