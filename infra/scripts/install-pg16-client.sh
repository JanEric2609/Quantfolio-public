#!/bin/sh
# One-shot: install postgresql-client-16 from PGDG on Debian 12 (bookworm).
# Run as root inside the app LXC (111).
set -e

KEYDIR=/usr/share/postgresql-common/pgdg
KEYFILE="${KEYDIR}/apt.postgresql.org.gpg"
LISTFILE=/etc/apt/sources.list.d/pgdg.list

install -d -m 0755 "$KEYDIR"

if [ ! -f "$KEYFILE" ]; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | gpg --dearmor -o "$KEYFILE"
fi

printf 'deb [signed-by=%s] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main\n' \
  "$KEYFILE" > "$LISTFILE"

echo "==> sources.list entry written:"
cat "$LISTFILE"

apt-get update -q
apt-get install -y postgresql-client-16

echo "==> Installed:"
pg_dump --version
