#!/bin/bash
# Let only the app LXC reach the Quantfolio database over the network. Run
# once on the db LXC, as root:
#
#   bash harden-postgres.sh            # app LXC at 10.0.0.21
#   APP_IP=10.0.0.5 bash harden-postgres.sh
#
# (Copy the file over, or run it straight from a checkout on the db LXC.)
#
# pg_hba.conf is first-match, so the script puts one block at the top: the
# Quantfolio database and role from APP_IP and loopback, then `reject` for the
# same database and role from every other IPv4 and IPv6 address. Rules further
# down (including wide `all`/`samenet` ones) then never apply to the app's role
# on its database, while other databases and roles keep working as before. Old lines just for the
# Quantfolio database and role are commented out. It reloads PostgreSQL (no
# restart), then replays pg_hba's first-match logic for APP_IP (must get in)
# and for outside IPv4/IPv6 addresses (must hit reject), and puts the file back
# if any check fails. Backup: pg_hba.conf.bak.<time>.
set -euo pipefail

APP_IP="${APP_IP:-10.0.0.21}"
DB_NAME="${DB_NAME:-quantfolio}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
psql_q() { runuser -u postgres -- psql -X -qtAc "$1"; }

[[ $EUID -eq 0 ]] || die "run as root on the db LXC"
[[ "$APP_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "APP_IP must be an IPv4 address (got '$APP_IP')"
[[ "$DB_NAME" =~ ^[A-Za-z0-9_]+$ ]] || die "DB_NAME may only contain letters, digits and _"
command -v psql >/dev/null || die "psql not found; is this the db LXC?"
command -v python3 >/dev/null || die "python3 is missing (apt-get install -y python3)"

hba="$(psql_q 'SHOW hba_file')"
[[ -f "$hba" ]] || die "pg_hba.conf not found"
role="$(psql_q "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$DB_NAME'")"
[[ -n "$role" ]] || die "database '$DB_NAME' not found; set DB_NAME"
[[ "$role" =~ ^[A-Za-z0-9_]+$ ]] || die "unexpected role name '$role'"
scram="$(psql_q "SELECT rolpassword LIKE 'SCRAM-SHA-256\$%' FROM pg_authid WHERE rolname = '$role'")"
method=md5
[[ "$scram" == "t" ]] && method=scram-sha-256

say "Database $DB_NAME, role $role, app LXC $APP_IP, method $method"
stamp="$(date +%Y%m%d%H%M%S)"
backup="$hba.bak.$stamp"
cp -p "$hba" "$backup"

rollback() {
  trap - ERR INT TERM
  set +e
  cp -p "$backup" "$hba"
  runuser -u postgres -- psql -X -qtAc 'SELECT pg_reload_conf()' >/dev/null
  echo "pg_hba.conf restored from $backup" >&2
  exit 1
}
trap rollback ERR INT TERM

python3 - "$hba" "$DB_NAME" "$role" "$APP_IP" "$method" <<'PY'
import sys
path, db, role, app_ip, method = sys.argv[1:6]
MARK = "# quantfolio (harden-postgres.sh)"
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
# Drop a block from an earlier run, and comment out old lines for exactly this database and role.
out, skipping = [], False
for line in lines:
    if line.startswith(MARK + " begin"):
        skipping = True
        continue
    if skipping:
        if line.startswith(MARK + " end"):
            skipping = False
        continue
    fields = line.split()
    if (len(fields) >= 4 and fields[0] in ("host", "hostssl", "hostnossl", "hostgssenc", "hostnogssenc")
            and fields[1].split(",") == [db] and fields[2].split(",") == [role]):
        out.append(f"# {line.rstrip()}   <- replaced by the quantfolio block at the top\n")
        continue
    out.append(line)
block = [
    f"{MARK} begin: only the app LXC and loopback reach {db}; first match wins\n",
    f"host\t{db}\t{role}\t{app_ip}/32\t{method}\n",
    f"host\t{db}\t{role}\t127.0.0.1/32\t{method}\n",
    f"host\t{db}\t{role}\t::1/128\t{method}\n",
    f"host\t{db}\t{role}\t0.0.0.0/0\treject\n",
    f"host\t{db}\t{role}\t::/0\treject\n",
    f"{MARK} end\n",
]
# Before the first rule, after the leading comment header.
first_rule = next((i for i, l in enumerate(out) if l.split() and not l.lstrip().startswith("#")), len(out))
open(path, "w", encoding="utf-8").write("".join(out[:first_rule] + block + out[first_rule:]))
print(f"wrote the quantfolio block ({method}) at line {first_rule + 1}")
PY

errors="$(psql_q "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")"
[[ "$errors" == "0" ]] || { psql_q "SELECT line_number, error FROM pg_hba_file_rules WHERE error IS NOT NULL" >&2; false; }
psql_q 'SELECT pg_reload_conf()' >/dev/null

# Replay first-match for TCP connections to the database as the role.
rules="$(runuser -u postgres -- psql -X -qtA -F'|' -c "SELECT line_number, type, array_to_string(database, ','),
  array_to_string(user_name, ','), COALESCE(address, ''), COALESCE(netmask, ''), auth_method
  FROM pg_hba_file_rules WHERE error IS NULL ORDER BY line_number")"
python3 - "$rules" "$DB_NAME" "$role" "$APP_IP" <<'PY'
import ipaddress, sys
rules, db, role, app_ip = sys.argv[1:5]

def first_match(ip: str) -> str | None:
    addr = ipaddress.ip_address(ip)
    for row in rules.splitlines():
        _line, kind, dbs, users, address, mask, method = row.split("|")
        if not kind.startswith("host"):
            continue
        if not ({db, "all"} & set(dbs.split(","))) or not ({role, "all"} & set(users.split(","))):
            continue
        if address in ("all",):
            return method
        if address in ("samehost", "samenet") or not address:
            continue  # resolved by the server; our reject lines come before any of these
        try:
            net = ipaddress.ip_network(f"{address}/{mask}" if mask else address, strict=False)
        except ValueError:
            continue
        if addr.version == net.version and addr in net:
            return method
    return None

problems = []
if first_match(app_ip) in (None, "reject"):
    problems.append(f"{app_ip} would not get in")
for outside in ("203.0.113.7", "10.0.0.250", "2001:db8::7"):
    if outside == app_ip:
        continue
    method = first_match(outside)
    if method not in (None, "reject"):
        problems.append(f"{outside} would still get in ({method})")
if problems:
    print("; ".join(problems), file=sys.stderr)
    sys.exit(1)
print(f"checked: {app_ip} gets in, other IPv4 and IPv6 addresses are rejected")
PY
trap - ERR INT TERM

listen="$(psql_q 'SHOW listen_addresses')"
say "Done. Only $APP_IP (and loopback) reach $DB_NAME as $role (backup: $backup)."
if [[ "$listen" == "*" ]]; then
  echo "Note: listen_addresses = '*'. That is fine with these rules; to also close the port on other"
  echo "interfaces set listen_addresses = 'localhost,<this LXC's LAN IP>' and restart PostgreSQL."
fi
echo "Check from the app LXC: curl -s http://127.0.0.1:8000/healthz   (db_up should be true)"
