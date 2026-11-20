---
name: deployment-check
description: Pre-flight validation for Quantfolio's multi-LXC Proxmox deployment — checks systemd units, Caddy config, database connectivity, and migration state across all 4 LXCs
disable-model-invocation: true
---

Run a full deployment pre-flight check for the Quantfolio multi-LXC topology.

## Topology

| LXC   | Role                  | Hostname / IP       |
|-------|-----------------------|----------------------|
| LXC-1 | PostgreSQL + Redis     | quantfolio-db       |
| LXC-2 | API + Scheduler worker | quantfolio-api      |
| LXC-3 | LLM (llama.cpp)        | quantfolio-llm      |
| LXC-4 | Web (Caddy + frontend) | quantfolio-web      |
| LXC-5 | OpenBB market data (REST :6900 + MCP :6950), optional | CTID 100 on prod |

**Prod CTID map (audit 2026-08 §0):** db=110, app=111, llm=112, web=113, OpenBB=100. A stopped `llamacpp` container (CTID 104) is dead weight — ignore it, flag for decommission.

### Systemd unit naming caveat

The repo-canonical units are `quantfolio-api` and `quantfolio-worker` (`infra/systemd/*.service`), but the **app LXC (111) has been observed with the typo'd name `quanfolio-app`** (audit 2026-08 §0). When checking units, accept either spelling and report the mismatch as pending ops cleanup — do not fail the whole pre-flight on the typo alone:

```bash
ssh quantfolio-api "systemctl list-units --type=service | grep -Ei 'qu[oa]nfolio'"
```

## Checks (in order)

### 1. Systemd service health

SSH into each LXC and verify critical services are active:

```bash
for lxc in quantfolio-db quantfolio-api quantfolio-llm quantfolio-web; do
  ssh "$lxc" "systemctl is-active postgresql redis-server quantfolio-api quantfolio-worker caddy 2>/dev/null" || echo "$lxc: FAIL"
done
```

Expected: all services show `active`.

### 2. Database connectivity

From the API LXC, verify Postgres and Redis are reachable:

```bash
ssh quantfolio-api "pg_isready -h $(grep DATABASE_URL /opt/quantfolio/.env | cut -d= -f2 | sed 's|.*@||;s|/.*||')" 
ssh quantfolio-api "redis-cli -h $(grep REDIS_URL /opt/quantfolio/.env | cut -d= -f2 | sed 's|.*@||;s|:.*||') ping"
```

Expected: `pg_isready` responds, Redis returns `PONG`.

### 3. Migration state

```bash
ssh quantfolio-api "cd /opt/quantfolio && .venv/bin/alembic current"
```

Expected: output matches the head revision (no pending migrations).

### 4. Caddy config validation

```bash
ssh quantfolio-web "caddy validate --config /etc/caddy/Caddyfile"
```

Expected: `Valid configuration` or exit 0.

### 5. Frontend build timestamp

```bash
ssh quantfolio-web "stat -c %y /var/www/quantfolio/index.html 2>/dev/null || echo 'frontend not built'"
```

Expected: a recent build timestamp (within days of deployment).

### 6. API health endpoint

```bash
curl -sf https://quantfolio.local/api/health | python3 -m json.tool
```

Expected: `{"status": "ok"}` or similar. Falls back to IP if hostname not resolved.

## Output format

```
PASS  service: lxc-name — service is active
FAIL  service: lxc-name — expected active but found inactive
PASS  db: pg_isready — accepting connections
FAIL  migration: head mismatch — expected X, found Y
```

End with a single-line verdict: **DEPLOYMENT HEALTHY** or **BLOCK: fix FAILs before proceeding**.
