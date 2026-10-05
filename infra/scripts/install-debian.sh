#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quantfolio}"
APP_USER="${APP_USER:-quantfolio}"

as_app_user() {
  sudo -H -u "${APP_USER}" "$@"
}

if [[ "${1:-}" == "--check" ]]; then
  command -v python3 >/dev/null
  command -v npm >/dev/null
  command -v psql >/dev/null
  command -v redis-server >/dev/null
  command -v caddy >/dev/null
  echo "QuantFolio deployment prerequisites are present."
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root on Debian 12." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip nodejs npm postgresql redis-server caddy rsync \
  ca-certificates curl gnupg lsb-release

# Docker — optional; only infra/docker-compose.yml uses it (the app itself runs on systemd).
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io
  usermod -aG docker "${APP_USER}" 2>/dev/null || true
fi

id "${APP_USER}" >/dev/null 2>&1 || useradd --system --create-home --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
mkdir -p "${APP_DIR}"
rsync -a --delete --exclude ".git" ./ "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

cd "${APP_DIR}/backend"
as_app_user python3 -m venv .venv
as_app_user "${APP_DIR}/backend/.venv/bin/python" -m pip install -U pip
as_app_user "${APP_DIR}/backend/.venv/bin/python" -m pip install --disable-pip-version-check -e ".[dkb_robo]"

cd "${APP_DIR}/frontend"
as_app_user npm ci
as_app_user npm run build

install -m 0644 "${APP_DIR}/infra/systemd/quantfolio-api.service" /etc/systemd/system/quantfolio-api.service
install -m 0644 "${APP_DIR}/infra/systemd/quantfolio-worker.service" /etc/systemd/system/quantfolio-worker.service
install -m 0644 "${APP_DIR}/infra/caddy/Caddyfile" /etc/caddy/Caddyfile
install -m 0750 "${APP_DIR}/infra/scripts/update-server.sh" /usr/local/sbin/quantfolio-update

systemctl daemon-reload
systemctl enable quantfolio-api quantfolio-worker caddy

echo "Create ${APP_DIR}/.env from .env.example (single node: API_HOST=127.0.0.1 and"
echo "FORWARDED_ALLOW_IPS=127.0.0.1, since Caddy runs on this host), run Alembic upgrade, then start services:"
echo "  cd ${APP_DIR}/backend && .venv/bin/alembic upgrade head"
echo "  systemctl start quantfolio-api quantfolio-worker caddy"
