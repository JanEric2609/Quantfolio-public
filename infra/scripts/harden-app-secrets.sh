#!/bin/bash
# Give Redis a password and replace a weak database password, then point the
# app at both. Run once on the app LXC, as root:
#
#   bash /opt/quantfolio/infra/scripts/harden-app-secrets.sh
#
# What it does (each step is skipped when already done):
#   1. Redis on this host: `requirepass` with a random 64-hex password, bound to
#      loopback, protected mode on; REDIS_URL in the app .env gets the password.
#   2. Database: if the password in DATABASE_URL is short or a placeholder
#      (or with --rotate-db-password), the app's own role sets a new random one
#      (`ALTER ROLE CURRENT_USER`, stored as SCRAM) and DATABASE_URL is updated.
#   3. Restarts quantfolio-api and quantfolio-worker and checks /healthz (the
#      database always; Redis only when it runs on this host).
# Anything failing after a change, or Ctrl-C, puts every file and password back
# as it was. Backups: <file>.bak.<timestamp> next to the .env and redis.conf.
# Passwords never appear on a command line (ps); they travel in the
# environment of root's own child processes.
set -euo pipefail
umask 077

ENV_FILE="${ENV_FILE:-/opt/quantfolio/.env}"
REDIS_CONF="${REDIS_CONF:-/etc/redis/redis.conf}"
PY="${PY:-/opt/quantfolio/backend/.venv/bin/python}"
ROTATE_DB=0
[[ "${1:-}" == "--rotate-db-password" ]] && ROTATE_DB=1

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root on the app LXC"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found; set ENV_FILE"
[[ -x "$PY" ]] || die "$PY not found; set PY to the app venv's python"
command -v openssl >/dev/null || die "openssl is missing (apt-get install openssl)"

stamp="$(date +%Y%m%d%H%M%S)"
env_backup="$ENV_FILE.bak.$stamp"
cp -p "$ENV_FILE" "$env_backup"
redis_backup=""
old_db_url=""
new_db_url=""

# Read one KEY=value line of the .env.
env_get() { "$PY" - "$ENV_FILE" "$1" <<'PY'
import sys
path, key = sys.argv[1], sys.argv[2]
for line in open(path, encoding="utf-8"):
    s = line.strip()
    if s.startswith(key + "="):
        print(s[len(key) + 1:].strip().strip('"').strip("'"))
        break
PY
}
# Rewrite one KEY=value line in place (keeps owner and mode); the value comes from $2
# but reaches Python through the environment, not its argv.
env_set() { ENV_VALUE="$2" "$PY" - "$ENV_FILE" "$1" <<'PY'
import os, sys
path, key, value = sys.argv[1], sys.argv[2], os.environ["ENV_VALUE"]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out, done = [], False
for line in lines:
    if line.strip().startswith(key + "=") and not done:
        out.append(f"{key}={value}\n")
        done = True
    else:
        out.append(line)
if not done:
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out.append(f"{key}={value}\n")
with open(path, "r+", encoding="utf-8") as fh:
    fh.seek(0)
    fh.write("".join(out))
    fh.truncate()
PY
}
# The password inside a redis:// or postgresql:// URL ($1), via the environment.
url_password() { URL="$1" "$PY" -c 'import os; from urllib.parse import urlsplit, unquote; print(unquote(urlsplit(os.environ["URL"]).password or ""))'; }

rollback() {
  trap - ERR INT TERM
  set +e
  printf '\n==> Rolling back\n' >&2
  if [[ -n "$new_db_url" && -n "$old_db_url" ]]; then
    NEW_URL="$new_db_url" OLD_URL="$old_db_url" "$PY" - <<'PY' >&2
import os
from sqlalchemy import create_engine, make_url, text
pw = make_url(os.environ["OLD_URL"]).password or ""
if pw:
    literal = pw.replace("'", "''")
    with create_engine(os.environ["NEW_URL"]).begin() as c:
        c.execute(text(f"ALTER ROLE CURRENT_USER WITH PASSWORD '{literal}'"))
    print("database password restored")
else:
    print("the old DATABASE_URL had no password; nothing to restore")
PY
  fi
  cp -p "$env_backup" "$ENV_FILE"
  if [[ -n "$redis_backup" ]]; then
    cp -p "$redis_backup" "$REDIS_CONF"
    systemctl restart redis-server
  fi
  systemctl restart quantfolio-api quantfolio-worker
  echo "Rolled back. Backups kept: $env_backup ${redis_backup}" >&2
  exit 1
}
arm_rollback() { trap rollback ERR INT TERM; }

healthz() {
  local host port body=""
  host="$(env_get API_HOST)"; port="$(env_get API_PORT)"
  [[ -z "$host" || "$host" == "0.0.0.0" ]] && host=127.0.0.1
  port="${port:-8000}"
  for _ in $(seq 1 30); do
    if body="$(curl -fsS --max-time 5 "http://$host:$port/healthz" 2>/dev/null)"; then
      NEED_REDIS="$need_redis" "$PY" -c 'import json,os,sys; c=json.loads(sys.argv[1])["checks"]; sys.exit(0 if c["db_up"] and (c["redis_up"] or os.environ["NEED_REDIS"] != "1") else 1)' "$body" \
        && return 0
    fi
    sleep 2
  done
  echo "last /healthz answer: ${body:-none}" >&2
  return 1
}

changed=0
# /healthz must show Redis up only when this host's Redis is in use (and so
# checked or changed below). Without a local Redis the app's default
# REDIS_URL is unreachable before and after, and that is not this script's to fix.
need_redis=0

# --- 1. Redis ---------------------------------------------------------------
redis_url="$(env_get REDIS_URL)"
if ! dpkg -s redis-server >/dev/null 2>&1 || [[ ! -f "$REDIS_CONF" ]]; then
  say "Redis is not installed on this host; skipping (REDIS_URL is ${redis_url:+set}${redis_url:-unset})"
else
  redis_host="$(URL="${redis_url:-redis://localhost:6379/0}" "$PY" -c 'import os; from urllib.parse import urlsplit; print(urlsplit(os.environ["URL"]).hostname or "")')"
  current_pw="$( [[ -n "$redis_url" ]] && url_password "$redis_url" || true)"
  if [[ -n "$redis_url" && ! "$redis_host" =~ ^(localhost|127\.0\.0\.1|::1)$ ]]; then
    say "REDIS_URL points at $redis_host, not this host; skipping Redis"
  elif grep -Eq '^[[:space:]]*requirepass[[:space:]]+[^[:space:]]+' "$REDIS_CONF" && [[ -n "$current_pw" ]] \
       && [[ "$(REDISCLI_AUTH="$current_pw" redis-cli -h 127.0.0.1 ping 2>/dev/null)" == PONG ]] \
       && [[ "$(redis-cli -h 127.0.0.1 ping 2>/dev/null)" != PONG ]]; then
    say "Redis already requires a password and REDIS_URL has it; nothing to do"
    need_redis=1
  else
    say "Setting a Redis password and keeping Redis on loopback"
    redis_backup="$REDIS_CONF.bak.$stamp"
    cp -p "$REDIS_CONF" "$redis_backup"
    arm_rollback
    pw="$(openssl rand -hex 32)"
    sed -i -E '/^[[:space:]]*(requirepass|bind|protected-mode)[[:space:]]/d' "$REDIS_CONF"
    printf '\n# Quantfolio (harden-app-secrets.sh, %s)\nbind 127.0.0.1 -::1\nprotected-mode yes\nrequirepass %s\n' \
      "$stamp" "$pw" >> "$REDIS_CONF"
    systemctl restart redis-server
    sleep 1
    [[ "$(REDISCLI_AUTH="$pw" redis-cli -h 127.0.0.1 ping 2>/dev/null)" == PONG ]]
    [[ "$(redis-cli -h 127.0.0.1 ping 2>&1)" != PONG ]]  # must now refuse without the password
    # Without REDIS_URL the app still uses its default redis://localhost:6379/0,
    # so it gets the password either way.
    new_url="$(BASE="${redis_url:-redis://localhost:6379/0}" PW="$pw" "$PY" - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit
u = urlsplit(os.environ["BASE"])
host = u.hostname or "localhost"
if ":" in host:  # IPv6 literal: keep the brackets, or the port can't be parsed
    host = f"[{host}]"
port = f":{u.port}" if u.port else ""
pw = os.environ["PW"]
print(urlunsplit((u.scheme or "redis", f":{pw}@{host}{port}", u.path or "/0", u.query, u.fragment)))
PY
)"
    env_set REDIS_URL "$new_url"
    changed=1
    need_redis=1
  fi
fi

# --- 2. Database password ---------------------------------------------------
old_db_url="$(env_get DATABASE_URL)"
[[ -n "$old_db_url" ]] || die "DATABASE_URL is not set in $ENV_FILE"
weak="$(DB_URL="$old_db_url" "$PY" - <<'PY'
import os
from sqlalchemy import make_url
u = make_url(os.environ["DB_URL"])
pw = u.password or ""
placeholders = ("password", "choose", "secure", "quantfolio", "changeme", "secret", "postgres")
print("yes" if u.get_backend_name() == "postgresql" and (len(pw) < 20 or any(p in pw.lower() for p in placeholders)) else "no")
PY
)"
if [[ "$weak" == "yes" || $ROTATE_DB -eq 1 ]]; then
  say "Replacing the database password (it was short or a placeholder, or you asked)"
  arm_rollback
  pw="$(openssl rand -hex 32)"
  new_db_url="$(DB_URL="$old_db_url" PW="$pw" "$PY" - <<'PY'
import os
from sqlalchemy import create_engine, make_url, text
url, pw = make_url(os.environ["DB_URL"]), os.environ["PW"]
with create_engine(url).begin() as conn:
    conn.execute(text("SET LOCAL password_encryption = 'scram-sha-256'"))
    conn.execute(text(f"ALTER ROLE CURRENT_USER WITH PASSWORD '{pw}'"))
print(url.set(password=pw).render_as_string(hide_password=False))
PY
)"
  env_set DATABASE_URL "$new_db_url"
  changed=1
else
  say "The database password looks strong; leaving it (use --rotate-db-password to replace it anyway)"
fi

# --- 3. Restart and verify ---------------------------------------------------
if [[ $changed -eq 1 ]]; then
  say "Restarting quantfolio-api and quantfolio-worker"
  arm_rollback
  systemctl restart quantfolio-api quantfolio-worker
  healthz || { echo "the API did not come back healthy" >&2; false; }
  trap - ERR INT TERM
  say "Done. The API reaches the database and Redis with the new passwords."
  echo "Backups (contain the OLD passwords, delete them once you're happy): $env_backup ${redis_backup}"
else
  rm -f "$env_backup"
  say "Nothing to change."
fi
