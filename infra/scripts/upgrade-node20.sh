#!/bin/sh
# Upgrade the web LXC to Node.js 20 (react-router 7 requires Node >=20).
# Infra-only: no application code changes. Run as root inside the web LXC (113).
# The next `quantfolio-update` rebuilds the frontend cleanly via `npm ci`.
set -e

TMP="$(mktemp)"
curl -fsSL https://deb.nodesource.com/setup_20.x -o "$TMP"
bash "$TMP"
rm -f "$TMP"

apt-get install -y nodejs

echo
echo "==> Node/npm now:"
node --version
npm --version
