# QuantFolio Multi-Node Deployment (Proxmox 4-LXC)

QuantFolio runs as a **canonical production deployment on four Debian 12 LXC containers** (db, app, llm, web) hosted on Proxmox VE 8.0+. This is the authoritative topology; local single-node dev setups are covered in the appendix.

## Topology

| LXC | CTID | Hostname | IP:Port | Role |
|---|---:|---|---|---|
| db | 110 | quantfolio-db | <DB_HOST>:5432 | PostgreSQL 16 + TimescaleDB + pgvector |
| app | 111 | quantfolio-api | <APP_HOST>:8000 | FastAPI API (`quantfolio-api` unit) + scheduler (`quantfolio-worker` unit) |
| llm | 112 | quantfolio-llm | <LLM_HOST>:8080 | llama.cpp with `Qwen3.6-35B-A3B` model; OpenAI-compatible at `/v1` endpoint; systemd unit `quantfolio-llamacpp` |
| web | 113 | quantfolio-web | <WEB_HOST> | Caddy reverse proxy + static frontend; proxies to app upstream `<APP_HOST>:8000` |

## Per-LXC Setup

Detailed step-by-step deployment instructions for each container are in **`infra/runbooks/deploy.md`**. This section provides a high-level summary of each LXC's role.

### Database (quantfolio-db)

PostgreSQL 16 with TimescaleDB and pgvector extensions. Stores all portfolio data, holdings, transactions, settings, encrypted secrets, and DKB sync logs.

**See** `infra/runbooks/deploy.md` Stage 1 for detailed PostgreSQL setup, user creation, and network configuration.

### App (quantfolio-api)

The FastAPI API server and APScheduler job runner. Houses both the `quantfolio-api` systemd unit (HTTP server listening on port 8000) and `quantfolio-worker` unit (scheduled jobs). Repository lives in `/opt/quantfolio`; the Python virtual environment is at `backend/.venv`.

**See** `infra/runbooks/deploy.md` Stage 3 for Debian setup, Python environment, .env creation, and systemd service installation.

**Scheduled jobs run ONLY in the `quantfolio-worker` process.** The API server does not execute background jobs; both services must be running for full functionality.

### LLM (quantfolio-llm)

llama.cpp compiled with optional NVIDIA GPU support. Serves model `Qwen3.6-35B-A3B` at `http://<LLM_HOST>:8080` with an OpenAI-compatible API at the `/v1` endpoint. Systemd unit is `quantfolio-llamacpp`.

**See** `infra/runbooks/deploy.md` Stage 2 for CMake build, model download, and service setup.

### Web (quantfolio-web)

Caddy reverse proxy + static frontend assets. Proxies HTTP/HTTPS traffic to the app upstream at `<APP_HOST>:8000` (set via `APP_UPSTREAM` environment variable or Caddyfile default). Serves the React frontend compiled from the `frontend/` directory.

**See** `infra/runbooks/deploy.md` Stage 4 for Caddyfile installation and TLS setup.

## App Environment Variables

On the app LXC (`/opt/quantfolio/.env`):

```bash
APP_ENV=production
DATABASE_URL=postgresql://quantfolio:<password>@<DB_HOST>:5432/quantfolio
LLM_LOCAL_URL=http://<LLM_HOST>:8080
LLM_API_KEY=<same value as LLAMA_API_KEY on the LLM LXC>
FRONTEND_ORIGIN=http://<WEB_HOST>
REDIS_URL=redis://:<redis password>@localhost:6379/0
JWT_SECRET=<openssl rand -hex 32>
ENCRYPTION_KEY=<Fernet key>
API_HOST=<APP_HOST>
FORWARDED_ALLOW_IPS=<WEB_HOST>
# SETUP_TOKEN=<optional, see "First-run setup token">
# ALLOWED_HOSTS=<APP_HOST>,127.0.0.1,localhost
```

Security-relevant variables:

| Variable | Meaning |
|---|---|
| `APP_ENV` | `production` on every server: Secure session cookie, startup secret checks, `/docs` `/redoc` `/openapi.json` switched off. `local` is for development on `http://localhost`. |
| `API_HOST` | Address `quantfolio-api` listens on (default `0.0.0.0`). Set the app LXC's LAN address so a second interface cannot reach the API around Caddy; the web LXC must still reach it. Replaces `API_HOST`, which the unit no longer reads. |
| `FORWARDED_ALLOW_IPS` | The proxy address(es) uvicorn trusts `X-Forwarded-For` from: the web LXC (default `<WEB_HOST>`; `127.0.0.1` on a single node). **Never `*`.** See "Client addresses and login throttling". |
| `SETUP_TOKEN`, `DATA_DIR` | First-run registration token and where the generated one is kept. See "First-run setup token". |
| `ALLOWED_HOSTS` | Optional comma-separated `Host` allow-list (no ports); requests for any other host get 400. Unset = off. Include `127.0.0.1` and `localhost` if health checks call the API directly. |
| `LLM_API_KEY` | Bearer key sent to a llama-server started with `LLAMA_API_KEY`. Alternatively store it as the Local LLM credential in Control Center. |
| `JWT_SECRET`, `ENCRYPTION_KEY` | Required outside local development (the API refuses to start without a non-placeholder `JWT_SECRET` and an `ENCRYPTION_KEY`). |

After changing the unit files or this `.env`, `quantfolio-update-app` installs updated `quantfolio-api`/`quantfolio-worker` units and restarts both; by hand: `cp infra/systemd/quantfolio-*.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart quantfolio-api quantfolio-worker`.

**`REDIS_URL`** is optional. It backs WebAuthn/passkey challenge storage (`services/auth.py`); when unset or unreachable the app falls back to in-memory challenges. Running Redis (co-located on the app LXC) is **recommended once you use passkeys**, so challenges are shared across the `quantfolio-api` and `quantfolio-worker` processes and survive restarts.

When `REDIS_URL` is set explicitly (environment or `.env`), the **market-data provider rate limiters** (`foundation/providers/rate_limiter.py`) also live in Redis (key prefix `quantfolio:ratelimit:`), so the API and the worker draw from one quota per vendor instead of one each. If Redis is unreachable the limiters fall back to the per-process window and retry Redis after 30 s; provider calls are never blocked on it.

**Tuning knobs** (all optional): `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (PostgreSQL connection pool per process, default 10 + 20 — the worker runs up to 16 job threads and a tracked job holds two connections) and `PRICE_BACKFILL_WORKERS` (symbols backfilled concurrently, default 4; 1 is sequential; SQLite always runs sequentially).

**`LLM_LOCAL_URL`** is read by the settings resolver at startup and can be overridden by the Control Center `llm_base_url` setting (no app restart required). Similarly, **`FRONTEND_ORIGIN`** (comma-separated allowed origins) is honored at runtime via the settings resolver; adding a new origin does not require a rebuild or restart.

## CORS and Frontend Origins

Set `FRONTEND_ORIGIN` to a comma-separated list of allowed origins in the app `.env`:

```bash
FRONTEND_ORIGIN=http://<WEB_HOST>,https://quantfolio.local,https://<machine>.<tailnet>.ts.net
```

The setting is resolved at request time; restart the `quantfolio-api` unit only if you modify `.env` directly (Control Center updates do not require a restart).

## Access & Authentication Tiers

The deployment supports three access tiers, each with different security and authentication capabilities:

### Tier 0 (Current): Plain HTTP by IP

**Access:** `http://<WEB_HOST>`  
**Context:** Non-secure (browsers treat it as such).  
**Authentication:** Password login **only**. WebAuthn/passkeys are disabled because browsers require a secure context (HTTPS).

> **Tier 0 and the session cookie.** With `APP_ENV=production` the session cookie is `Secure`, and a browser
> does not keep a `Secure` cookie received over plain HTTP: sign-in appears to succeed and the next request is
> 401. Plain HTTP therefore only works with `COOKIE_SECURE=false` (or `APP_ENV=local`), which also sends the
> cookie, the password and every API response in clear across the LAN. Treat it as a stop-gap and move to
> Tier 2 below.

### Tier 1: Self-Signed HTTPS on LAN Hostname

**Access:** `https://quantfolio.local`  
**Context:** Secure context (self-signed certificate).  
**Authentication:** Passkeys enabled. Relying party ID (rp_id) = `quantfolio.local`.

Generate a self-signed certificate on the web LXC:

```bash
openssl req -x509 -newkey rsa:4096 -keyout /etc/caddy/quantfolio.key \
  -out /etc/caddy/quantfolio.crt -days 365 -nodes \
  -subj "/CN=quantfolio.local"
```

Update `/etc/caddy/Caddyfile` to serve HTTPS on `quantfolio.local:443` (see `infra/caddy/Caddyfile` for the template).

Clients must trust the self-signed cert or use a browser flag to bypass validation.

### Tier 2 (Recommended): Tailscale + MagicDNS

**Access:** `https://<machine>.<tailnet>.ts.net` (e.g., `https://quantfolio-web.example.ts.net`)  
**Context:** Secure context (real TLS provisioned by Tailscale).  
**Authentication:** Passkeys enabled. Relying party ID = the `.ts.net` hostname (stable across devices in the tailnet).

**Steps:**

1. Add the LXCs (or Proxmox host) to your Tailscale network.
2. Update `FRONTEND_ORIGIN` in the app `.env` to include the `.ts.net` hostname:
   ```bash
   FRONTEND_ORIGIN=http://<WEB_HOST>,https://<machine>.<tailnet>.ts.net
   ```
3. Give the **web LXC** HTTPS with Tailscale serve (Caddy keeps serving plain HTTP on `:80` behind it):
   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:80
   ```
   `tailscaled` inside an LXC needs the TUN device. On the Proxmox host add to `/etc/pve/lxc/113.conf`
   (and reboot the container):
   ```
   lxc.cgroup2.devices.allow: c 10:200 rwm
   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
   ```
   Enable HTTPS certificates for the tailnet once in the Tailscale admin console (DNS → HTTPS Certificates).
4. Switch the app to production semantics: `APP_ENV=production` in the app `.env` (Secure session cookie,
   startup secret checks), put the `.ts.net` origin into `FRONTEND_ORIGIN`, and set
   `webauthn_rp_id` to the `.ts.net` hostname in Control Center → Security & access, so passkeys work on that origin.
5. Let the real client address reach the API through both proxies (see below): Caddy must trust
   Tailscale serve's `X-Forwarded-For`.
6. Access from any device in the tailnet via the stable hostname.

Tailscale provides end-to-end encryption, stable DNS, and zero-trust authentication without opening ports to the public internet.

### Client addresses and login throttling

Every browser reaches the API through Caddy (and, in Tier 2, through Tailscale serve first), so the API
only ever sees the proxy's address unless uvicorn is told which proxies to believe. `quantfolio-api` runs with
`--proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"`; with `FORWARDED_ALLOW_IPS` set to the web LXC,
uvicorn replaces the connection address with the client address from `X-Forwarded-For`. Two things depend on it:

* the per-address rate limits (`5/minute` on login, register and passkey verification, `20/minute` on passkey options);
* the **login lockout**: 5 failures per **(username, client address)** in 15 minutes lock that pair out, and 50
  failures per username across all addresses slow distributed guessing. Someone hammering your username from
  elsewhere no longer locks you out; if the proxy is not trusted, every client shares one address and the
  lockout degrades to a single bucket per username.

When Tailscale serve fronts Caddy, Caddy sees tailscaled on `127.0.0.1` and, by default, throws away the
`X-Forwarded-For` it was given. Tell it to trust that hop by adding a global options block at the **top**
of `/etc/caddy/Caddyfile` on the web LXC (the repository Caddyfile is deliberately not changed), then
`caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy`:

```
{
	servers {
		trusted_proxies static 127.0.0.1
	}
}
```

Check it: sign in with a wrong password from a phone and a laptop and confirm in
`journalctl -u quantfolio-api` that the blocked-request warnings carry different addresses; or call
`curl -s -H 'X-Forwarded-For: 203.0.113.9' http://<APP_HOST>:8000/health` from a machine that is **not** in
`FORWARDED_ALLOW_IPS` — the header must have no effect there.

## Caddy Reverse Proxy

The web LXC runs Caddy, configured in `/etc/caddy/Caddyfile`. Caddy proxies incoming requests to the app upstream:

```
reverse_proxy <APP_HOST>:8000
```

The upstream address is set via the `APP_UPSTREAM` environment variable (defaults to `<APP_HOST>:8000` if not specified). Caddy also serves the static frontend assets built from the `frontend/` directory.

**Note:** The web LXC does **not** run any local API server; all requests to `/api/*` are proxied to the app upstream.

## First-run setup token

An empty database belongs to whoever registers first, and the API is reachable by anything that can reach
the web LXC. So the **first registration needs a one-time setup token** that only the operator can see:

* `SETUP_TOKEN` in the app `.env`, if you set one; otherwise
* a random token generated at the first start, logged at `WARNING` as `FIRST-RUN SETUP: ... setup token: <token>`
  (`journalctl -u quantfolio-api | grep 'FIRST-RUN SETUP'`) and written, mode `0600`, to
  `<data dir>/setup_token`.

The data dir is `DATA_DIR` if set, else `/var/lib/quantfolio` when it exists and is writable, else
`/opt/quantfolio/.quantfolio`. Enter the token on the registration form (login page or setup wizard). It is
compared in constant time, accepted only while no user exists, and the file is deleted after the first account
is created; a second registration answers `409` whatever it sends. The first account gets the `admin` role.
A wiped or restored-empty database starts the cycle again with a new token.

`/api/setup/status` and `/api/auth/status` tell an anonymous caller only whether the instance is initialised.
Accounts created before this change kept the role `user`. Migration `0125_admin_sole_user` makes the account
admin when it is the only one and nobody is admin yet (the Discover admin and repair endpoints and the Scalable
setup in the Control Center require it). With several accounts, promote one by hand:
`UPDATE users SET role = 'admin' WHERE username = '<you>';` in `psql`.

## Telegram bot: polling or webhook

`telegram_mode` (Control Center → Notes & alerts → Telegram bot) decides how messages reach the bot:

* **`polling` (default, recommended).** The worker long-polls Telegram's `getUpdates` (`telegram_polling` job,
  one run per minute, ~50 s of long polling each), so no public HTTPS endpoint is needed. It deletes any webhook
  first and keeps its update offset in `app_settings`, so a restart neither replays nor skips messages.
  Needs only the bot token and a worker restart after the upgrade.
* **`webhook`.** Telegram calls `https://<origin>/api/telegram/webhook`. Choosing this mode (or saving the
  bot token while it is chosen) creates the webhook secret and registers the first `https://` entry of
  `FRONTEND_ORIGIN` with Telegram; the endpoint verifies Telegram's secret header and answers `403` otherwise.
  It needs a publicly reachable HTTPS origin, which the tailnet-only Tier 2 is not.

The bot token and the webhook secret are stored encrypted (like the DKB login name, they used to sit in plaintext
in `api_keys.meta_json` and are moved into the encrypted value the first time they are read).

## Rotate keys that were logged before the URL-logging fix

Until the fix in PR #304, `httpx` logged every request URL at INFO. Provider API keys sent as query parameters
(Alpha Vantage, Finnhub, Twelve Data) and the **Telegram bot token** (it is part of the Bot API path) therefore
sit in old journal and log files. After deploying:

1. Create new keys/tokens: Alpha Vantage, Finnhub, Twelve Data in the providers' consoles; for Telegram,
   `/revoke` in @BotFather and take the new token.
2. Save them in Control Center (Market data, Notes & alerts) and run each connection's Test.
3. Revoke the old ones at the provider.
4. Drop the old logs: `journalctl --rotate && journalctl --vacuum-time=1s` on the app LXC (and delete any
   exported log files or Sentry events from that period).

## Network hardening checklist

* Postgres, Redis and (local development) docker-compose: loopback only, passwords required
  (`infra/docker-compose.yml`, `infra/.env.compose.example`). On the db LXC restrict `pg_hba.conf` to the app LXC;
  on the app LXC set `bind 127.0.0.1` and `requirepass` in `redis.conf` and put the password in `REDIS_URL`.
  Two scripts do this, each with a backup, a health check and an automatic rollback:
  `bash /opt/quantfolio/infra/scripts/harden-app-secrets.sh` on the app LXC (Redis password, and a new
  random database password when the current one is short or a placeholder, both written into `.env`;
  `--rotate-db-password` forces the latter) and `bash harden-postgres.sh` on the db LXC (puts a block at
  the top of `pg_hba.conf` that lets the app's role reach its database only from the app LXC and loopback and
  rejects it from every other IPv4/IPv6 address, then replays pg_hba's first-match for the app and for outside
  addresses; `APP_IP=…` to override <APP_HOST>). Passwords never appear on a command line, and Ctrl-C rolls
  back too. If PostgreSQL logs statements (`log_statement = ddl` or `all`), the `ALTER ROLE` with the new
  password lands in its log.
* `quantfolio-llamacpp`: set `LLAMA_BIND_HOST` (the LLM LXC's LAN address) and `LLAMA_API_KEY` in
  `/etc/quantfolio/llama.env`, mirror the key as `LLM_API_KEY` in the app `.env`. `/health` (and llama-server's
  model-list endpoints) stay open; chat, completions, embeddings and `/metrics` require the key.
  The unit no longer loads the app `.env`.
* `quantfolio-api`: `API_HOST`, `FORWARDED_ALLOW_IPS`, optional `ALLOWED_HOSTS`. Health probes must use the
  address it listens on (`curl http://<APP_HOST>:8000/health/ready`), not `127.0.0.1`, once `API_HOST` is narrowed.
* `/healthz` is unauthenticated and makes no outbound request (database, Redis and schema checks only); the LLM
  probe is the authenticated `GET /api/finagent/llm/health`.
* Cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE` that the browser marks cross-site (`Sec-Fetch-Site: cross-site`,
  or an `Origin` that is neither the request's own host nor in `FRONTEND_ORIGIN`) are answered `403`. curl and
  other clients that send neither header are unaffected, as is Telegram's webhook.
* `smtp_allow_plaintext` (Control Center → E-mail, advanced) is off: mail is only sent over STARTTLS or SSL.
* systemd sandboxing: the API and worker units set `ProtectSystem=full` and `ProtectControlGroups=true`; the llama
  unit adds `ProtectKernelTunables`, `RestrictSUIDSGID`, `PrivateTmp` and `NoNewPrivileges`. The API and worker
  deliberately omit `NoNewPrivileges`, `ProtectHome`, `ProtectSystem=strict`, `RestrictSUIDSGID` (they
  `sudo -n -u scalable-cli-user` and that user's home must stay writable), every seccomp setting such as
  `ProtectKernelTunables` (for a non-root `User=` systemd then turns `NoNewPrivileges` on by itself; see
  `docs/scalable.md`, systemd units) and `PrivateTmp` (the worker and API share `/tmp/quantfolio_*`). After
  installing the units, confirm the broker adapter still runs under them.
* Model artefacts (`/opt/quantfolio/ml-models`) are verified against a SHA-256 recorded at save time before
  they are unpickled; copy the `.sha256` files along with the models when restoring a backup. Artefacts from before
  this check are recorded on first load (look for `has no recorded SHA-256` in the log).

## Systemd Services

**App LXC (quantfolio-api):**

- **`quantfolio-api`** — FastAPI server on port 8000. HTTP request handler and REST API.
- **`quantfolio-worker`** — APScheduler background job runner. **The ONLY process that executes scheduled jobs** (recommendations refresh, experiment runs, prompt audit cleanup). Must be running for recurring work.

**LLM LXC (quantfolio-llm):**

- **`quantfolio-llamacpp`** — llama.cpp server on port 8080. OpenAI-compatible completions API.

**Web LXC (quantfolio-web):**

- **`caddy`** — Reverse proxy and TLS termination.

Start/restart all required services after an update:

```bash
systemctl restart quantfolio-api quantfolio-worker
systemctl restart quantfolio-llamacpp
systemctl reload-or-restart caddy
```

Verify health:

```bash
curl http://<APP_HOST>:8000/health/ready   # your API_HOST
curl http://<LLM_HOST>:8080/health         # your LLAMA_BIND_HOST (/health needs no API key)
```

## Updates and Maintenance

**Do not delete the database or rerun setup from scratch.** QuantFolio upgrades are applied via Alembic migrations.

### One-Command Updates

On the **Proxmox host**, install the orchestrator script once:

```bash
cd /opt/quantfolio
install -m 0750 infra/scripts/update-stack.sh /usr/local/sbin/quantfolio-update
pct set 113 -memory 2048 -swap 1024
pct reboot 113
```

Then run:

```bash
quantfolio-update --check
quantfolio-update [--with-llm]
```

The orchestrator:

1. Runs host preflight checks for `pct`, `curl`, db/app/web LXC state, and installed per-LXC updater commands. `--with-llm` also checks the llm LXC.
2. Runs `quantfolio-update-app` on app LXC (111): backs up the database, pulls code, installs backend deps, runs Alembic migrations, restarts `quantfolio-api` and `quantfolio-worker`, verifies database connectivity and `:8000/health/ready`.
3. Runs `quantfolio-update-web` on web LXC (113): pulls code, validates `frontend/package-lock.json`, rebuilds frontend (`npm ci && npm run build`), validates Caddy config, reloads Caddy, verifies web health.
4. Optionally with `--with-llm`, runs `quantfolio-update-llm` on llm LXC (112): pulls code, refreshes systemd unit if changed, restarts `quantfolio-llamacpp`, verifies `:8080/health`.
5. Performs a final end-to-end health check (`curl http://<WEB_HOST>/health` from the host).

**Environment overrides** (set before running `quantfolio-update`):

```bash
export DB_CTID=110        # Database container ID
export APP_CTID=111       # App container ID
export LLM_CTID=112       # LLM container ID (if --with-llm used)
export WEB_CTID=113       # Web container ID
export WEB_HOST=<WEB_HOST>  # Web host (bare host/IP; the final check hits http://$WEB_HOST/health)
```

**Web updater overrides** (set inside the web LXC when needed):

```bash
export APP_UPSTREAM=<APP_HOST>:8000       # App API upstream used for the web-stage health probe
export HEALTH_URL=http://<APP_HOST>:8000/health
export WEB_PROBE_URL=https://<machine>.<tailnet>.ts.net/  # Optional: verify the public SPA URL
```

By default, `quantfolio-update-web` probes the app upstream directly and validates the built `frontend/dist/index.html` artifact. This avoids false failures on HTTPS/Tailscale deployments where `http://127.0.0.1/` returns an automatic HTTPS redirect instead of the SPA HTML.

### Installing the Updater Scripts

**One-time install for each LXC (run inside the container after `git pull`):**

```bash
# App LXC (111)
cd /opt/quantfolio
chown -R quantfolio:quantfolio /opt/quantfolio
install -m 0750 infra/scripts/update-app.sh /usr/local/sbin/quantfolio-update-app

# Web LXC (113)
cd /opt/quantfolio
chown -R quantfolio:quantfolio /opt/quantfolio
install -m 0750 infra/scripts/update-web.sh /usr/local/sbin/quantfolio-update-web

# LLM LXC (112, optional)
cd /opt/quantfolio
chown -R quantfolio:quantfolio /opt/quantfolio
install -m 0750 infra/scripts/update-llm.sh /usr/local/sbin/quantfolio-update-llm
```

**One-time install for the orchestrator (run on the Proxmox host after `git pull`):**

```bash
cd /opt/quantfolio
install -m 0750 infra/scripts/update-stack.sh /usr/local/sbin/quantfolio-update
```

If the web LXC was deployed before the Caddy upstream default was fixed, patch the active Caddyfile once:

```bash
sed -i 's/{$APP_UPSTREAM:localhost:8000}/{$APP_UPSTREAM:<APP_HOST>:8000}/g' /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload-or-restart caddy
```

### Manual Update Flow

If needed, update manually per LXC.

**App LXC (111)** -- backend and migrations only:

```bash
cd /opt/quantfolio
runuser -u quantfolio -- git pull --ff-only

cd backend
runuser -u quantfolio -- .venv/bin/python -m pip install --disable-pip-version-check -e ".[dkb_robo]"
runuser -u quantfolio -- .venv/bin/alembic upgrade head
cd ..

systemctl restart quantfolio-api quantfolio-worker
curl --fail http://<APP_HOST>:8000/health/ready   # your API_HOST (127.0.0.1 on a single node)
```

**Web LXC (113)** -- frontend build and Caddy only:

```bash
cd /opt/quantfolio
runuser -u quantfolio -- git pull --ff-only

cd frontend
runuser -u quantfolio -- npm ci
runuser -u quantfolio -- env NODE_OPTIONS=--max-old-space-size=1536 npm run build
cd ..

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload-or-restart caddy
curl --fail http://<APP_HOST>:8000/health
grep -q 'id="root"' /opt/quantfolio/frontend/dist/index.html
```

**Note:** The Caddyfile in `/etc/caddy/` is **not** overwritten by the updater; it is a static configuration file set during Stage 4 deployment.

## Optional Features

### Market Data Providers

Provider priority is **data-driven**: the request order comes from the `provider_chain_json` setting (Control Center → Market data → Priority), not a hardcoded fallback list. Providers that cannot serve a symbol are skipped via per-provider dead-cooldowns instead of being retried forever.

Optional OpenBB service (not part of core deployment):

```bash
OPENBB_API_URL=http://127.0.0.1:6900
```

Enable OpenBB in Settings after the service is reachable. Provider health:

```
GET /api/market/providers/status
```

### Research & Backtesting

Optional heavy-dependency features, switched by environment variables:

```bash
FINRL_ENABLED=true                    # FinRL reinforcement learning (CPU-only PPO), on by default
QLIB_ENABLED=false                    # legacy switch; the Qlib integration was removed
MLFLOW_TRACKING_DIR=/opt/quantfolio/mlruns  # MLflow tracking
```

The Lean integration was removed; live trading is not implemented anywhere.

### DKB FinTS Integration

QuantFolio includes an optional DKB web adapter for read-only FinTS account syncs.

**No automated sync jobs exist.** DKB syncs are manual only and require user-initiated approval via the DKB app (decoupled pushTAN).

Install the optional DKB adapter on first deploy:

```bash
cd /opt/quantfolio/backend
runuser -u quantfolio -- .venv/bin/pip install -e ".[dkb_robo]"
systemctl restart quantfolio-api quantfolio-worker
```

Use DKB's FinTS endpoint:

- **BLZ:** `12030000`
- **Endpoint:** `https://fints.dkb.de/fints`
- **Security medium:** DKB-App (decoupled pushTAN)

## Backups

Set up automated daily database backups:

```bash
install -m 0750 infra/scripts/backup-db.sh /usr/local/sbin/quantfolio-backup-db
```

Add to the app LXC crontab:

```bash
0 2 * * * DATABASE_URL="$(grep '^DATABASE_URL=' /opt/quantfolio/.env | cut -d= -f2-)" /usr/local/sbin/quantfolio-backup-db
```

## German Tax Planning

Settings include tax residency, church tax, Freistellungsauftrag configuration, and tax estimation toggles. All tax outputs are labelled `estimate: True` and `not_tax_advice: True`. **Broker statements remain the source of truth.**

---

## Appendix: Local Single-Node Dev (Non-Canonical)

For local development on a single machine (not recommended for production), clone the repository and run both backend and frontend:

```bash
git clone https://github.com/JanEric2609/Quantfolio-public.git
cd Quantfolio

# Backend: create venv and start dev server (port 8000)
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload  # dev server on port 8000

# Frontend: run Vite dev server (port 5173)
cd ../frontend
npm install
npm run dev
```

For the backend, use an in-memory SQLite database (tests) or a local PostgreSQL instance (full features). Use `.env.example` or `.env` in the repo root with `APP_ENV=development`.

**This setup is for development only and does not reflect the production multi-node architecture.**
