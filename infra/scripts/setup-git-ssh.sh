#!/bin/sh
# Switch the quantfolio checkout from HTTPS to an SSH deploy key so the
# updater can fetch/pull without interactive GitHub credentials.
# Run as root inside an LXC that has the /opt/quantfolio checkout (111, 113).
set -e

APP_USER="${APP_USER:-quantfolio}"
APP_DIR="${APP_DIR:-/opt/quantfolio}"
SSH_URL="${SSH_URL:-git@github.com:JanEric2609/Quantfolio-public.git}"

HOME_DIR="$(getent passwd "$APP_USER" | cut -d: -f6)"
[ -n "$HOME_DIR" ] || { echo "User $APP_USER not found"; exit 1; }
[ -d "$APP_DIR/.git" ] || { echo "$APP_DIR is not a git checkout"; exit 1; }

SSH_DIR="$HOME_DIR/.ssh"
KEYFILE="$SSH_DIR/id_ed25519"
KNOWN="$SSH_DIR/known_hosts"

install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$SSH_DIR"

# Generate a key only if one does not already exist (idempotent).
if [ ! -f "$KEYFILE" ]; then
  runuser -u "$APP_USER" -- ssh-keygen -t ed25519 -N "" -f "$KEYFILE" \
    -C "$APP_USER@$(hostname)-quantfolio-deploy"
fi

# Trust github.com's host keys so the first connection is non-interactive.
if ! runuser -u "$APP_USER" -- ssh-keygen -F github.com -f "$KNOWN" >/dev/null 2>&1; then
  ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> "$KNOWN" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$KNOWN"
  chmod 0644 "$KNOWN"
fi

# Point origin at SSH instead of HTTPS (updater uses `git ... origin`).
runuser -u "$APP_USER" -- git -C "$APP_DIR" remote set-url origin "$SSH_URL"

echo
echo "==> origin is now:"
runuser -u "$APP_USER" -- git -C "$APP_DIR" remote get-url origin
echo
echo "==> Add this PUBLIC key as a READ-ONLY Deploy key on GitHub:"
echo "    Repo -> Settings -> Deploy keys -> Add deploy key"
echo "    Leave 'Allow write access' UNCHECKED (the updater only fetches)."
echo
cat "$KEYFILE.pub"
echo
echo "Then verify (should NOT prompt for a password):"
echo "    runuser -u $APP_USER -- git -C $APP_DIR fetch origin"
