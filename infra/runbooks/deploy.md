# QuantFolio Deployment Runbook

**Reference:** Phase 9 infra deployment on Proxmox LXC with GPU passthrough.

## Prerequisites

- Proxmox VE 8.0+ host with IOMMU enabled (see `gpu-passthrough.md`)
- Qwen3.6-35B-A3B-UD-Q4_K_M.gguf model file (~18-20 GB)
- Domain name (optional; self-signed works for LAN)
- ~22-25 GB available storage on Proxmox

## Quick Start (30 min)

1. **Run bootstrap script as root:**
   ```bash
   cd /root/quantfolio/infra/scripts
   ./bootstrap.sh
   ```

2. **Create LXC containers** (via Proxmox Web UI or CLI):
   ```bash
   pct create 110 local-lvm debian-12-standard_12.0-1_amd64.tar.zst \
     -hostname quantfolio-db -cores 4 -memory 4096 -net0 name=eth0,bridge=vmbr0,ip=<DB_HOST>/24
   pct create 111 local-lvm debian-12-standard_12.0-1_amd64.tar.zst \
     -hostname quantfolio-api -cores 4 -memory 2048 -net0 name=eth0,bridge=vmbr0,ip=<APP_HOST>/24
   pct create 112 local-lvm debian-12-standard_12.0-1_amd64.tar.zst \
     -hostname quantfolio-llm -cores 8 -memory 16384 -net0 name=eth0,bridge=vmbr0,ip=<LLM_HOST>/24
   pct create 113 local-lvm debian-12-standard_12.0-1_amd64.tar.zst \
     -hostname quantfolio-web -cores 2 -memory 2048 -swap 1024 -net0 name=eth0,bridge=vmbr0,ip=<WEB_HOST>/24
   ```

3. **Add GPU passthrough** (if available):
   ```bash
   # On Proxmox host, edit /etc/pve/lxc/112.conf:
   hostpci0: 01:00.0,pcie=1
   ```

4. **Start containers:**
   ```bash
   pct start 110 111 112 113
   ```

## Step-by-Step Deployment

### Stage 1: Database Container (quantfolio-db, ID 110)

1. **SSH into container:**
   ```bash
   pct exec 110 bash
   ```

2. **Update and install PostgreSQL 16:**
   ```bash
   apt-get update && apt-get install -y postgresql-16 postgresql-contrib-16 build-essential
   ```

3. **Install TimescaleDB:**
   ```bash
   apt-get install -y postgresql-16-timescaledb-2-loader postgresql-16-timescaledb
   # Edit /etc/postgresql/16/main/postgresql.conf:
   # shared_preload_libraries = 'timescaledb'
   systemctl restart postgresql
   ```

4. **Install pgvector:**
   ```bash
   apt-get install -y postgresql-16-pgvector
   ```

5. **Create database and user:**
   ```bash
   runuser -u postgres -- psql << EOF
   CREATE USER quantfolio_user WITH PASSWORD 'choose_secure_password';
   CREATE DATABASE quantfolio OWNER quantfolio_user;
   \c quantfolio
   CREATE EXTENSION IF NOT EXISTS timescaledb;
   CREATE EXTENSION IF NOT EXISTS vector;
   GRANT ALL ON SCHEMA public TO quantfolio_user;
   EOF
   ```

6. **Configure PostgreSQL for remote connections:**
   ```bash
   # Edit /etc/postgresql/16/main/postgresql.conf:
   listen_addresses = '*'
   
   # Edit /etc/postgresql/16/main/pg_hba.conf:
   host    quantfolio     quantfolio_user  <LAN_CIDR>   md5
   ```

7. **Restart PostgreSQL:**
   ```bash
   systemctl restart postgresql
   ```

8. **Verify from Proxmox host:**
   ```bash
   psql -h <DB_HOST> -U quantfolio_user -d quantfolio -c "SELECT version();"
   ```

### Stage 2: LLM Container (quantfolio-llm, ID 112)

1. **SSH into container:**
   ```bash
   pct exec 112 bash
   ```

2. **Install dependencies:**
   ```bash
   apt-get update && apt-get install -y curl build-essential cmake git
   ```

3. **Install NVIDIA driver (if GPU available):**
   ```bash
   apt-get install -y nvidia-driver-550
   nvidia-smi  # Verify GPU is visible
   ```

4. **Clone and build llama.cpp:**
   ```bash
   cd /opt
   git clone https://github.com/ggerganov/llama.cpp.git
   cd llama.cpp
   mkdir build && cd build
   cmake .. -DLLAMA_CUDA=ON  # Or -DLLAMA_HIPBLAS=ON for AMD
   make -j$(nproc)
   ```

5. **Create model directory and download GGUF:**
   ```bash
   mkdir -p /var/lib/quantfolio/models
   cd /var/lib/quantfolio/models
   
   # Download model (example using Hugging Face CLI)
   huggingface-cli download Qwen/Qwen3.6-35B-A3B-UD-Q4_K_M \
     --local-dir . --local-dir-use-symlinks False
   ```

   Or use `wget`/`curl` if huggingface-cli not available.

6. **Create quantfolio user and app checkout:**
   ```bash
   useradd -m -s /bin/bash quantfolio
   chown -R quantfolio:quantfolio /var/lib/quantfolio /opt/llama.cpp
   cd /opt
   git clone https://github.com/JanEric2609/Quantfolio-public.git quantfolio
   chown -R quantfolio:quantfolio /opt/quantfolio
   ```

7. **Create the llama-server settings and copy the systemd service:**
   ```bash
   install -d -m 0750 -o root -g quantfolio /etc/quantfolio
   install -m 0640 -o root -g quantfolio /dev/null /etc/quantfolio/llama.env
   cat > /etc/quantfolio/llama.env << EOF
   LLAMA_API_KEY=$(openssl rand -hex 32)
   LLAMA_BIND_HOST=<LLM_HOST>
   EOF
   # Put the same key into the app LXC's /opt/quantfolio/.env as LLM_API_KEY.
   cp /opt/quantfolio/infra/systemd/quantfolio-llamacpp.service /etc/systemd/system/
   install -m 0750 /opt/quantfolio/infra/scripts/update-llm.sh /usr/local/sbin/quantfolio-update-llm
   systemctl daemon-reload
   systemctl enable quantfolio-llamacpp
   systemctl start quantfolio-llamacpp
   ```

8. **Verify service:**
   ```bash
   systemctl status quantfolio-llamacpp
   curl http://<LLM_HOST>:8080/health   # the LLAMA_BIND_HOST you set above (/health needs no key)
   ```

### Stage 3: App Container (quantfolio-api, ID 111)

1. **SSH into container:**
   ```bash
   pct exec 111 bash
   ```

2. **Install Python 3.11+ and updater dependencies:**
   ```bash
   apt-get update && apt-get install -y python3.11 python3.11-venv python3-pip git curl ca-certificates gnupg
   ```

   **Install PostgreSQL 16 client** (must match the DB server version — pg_dump 15 cannot dump from pg16):
   ```bash
   install -d -m 0755 /usr/share/postgresql-common/pgdg
   curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
     | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg
   tee /etc/apt/sources.list.d/pgdg.list <<'EOF'
   deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main
   EOF
   apt-get update && apt-get install -y postgresql-client-16
   ```

3. **Create quantfolio user and clone app repository:**
   ```bash
   useradd -m -s /bin/bash quantfolio
   cd /opt
   git clone https://github.com/JanEric2609/Quantfolio-public.git quantfolio
   cd quantfolio
   chown -R quantfolio:quantfolio /opt/quantfolio
   ```

4. **Create venv and install dependencies:**
   ```bash
   runuser -u quantfolio -- python3.11 -m venv backend/.venv
   runuser -u quantfolio -- backend/.venv/bin/pip install -e "backend[dev]"
   ```

5. **Create .env file:**
   ```bash
   cat > .env << EOF
   DATABASE_URL=postgresql://quantfolio_user:password@<DB_HOST>:5432/quantfolio
   REDIS_URL=redis://localhost:6379
   LLM_LOCAL_URL=http://<LLM_HOST>:8080
   LLM_API_KEY=        # Same value as LLAMA_API_KEY on the LLM LXC (Stage 2)
   ANTHROPIC_API_KEY=  # Optional
   OPENAI_API_KEY=     # Optional
   APP_ENV=production
   LOG_LEVEL=info
   FRONTEND_ORIGIN=http://<WEB_HOST>
   JWT_SECRET=$(openssl rand -hex 32)
   ENCRYPTION_KEY=$(backend/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
   API_HOST=<APP_HOST>
   FORWARDED_ALLOW_IPS=<WEB_HOST>
   EOF
   chown quantfolio:quantfolio .env && chmod 600 .env
   ```

   `API_HOST` keeps the API off any other interface (it must stay reachable from the web LXC);
   `FORWARDED_ALLOW_IPS` is the web LXC, whose `X-Forwarded-For` uvicorn then trusts so the login
   lockout and rate limits see real client addresses (see `docs/deployment.md`, "Client addresses and login throttling").

6. **Apply migrations:**
   ```bash
   cd backend
   runuser -u quantfolio -- .venv/bin/alembic upgrade head
   cd ..
   ```

7. **Copy systemd services and updater:**
   ```bash
   cp infra/systemd/quantfolio-api.service /etc/systemd/system/
   cp infra/systemd/quantfolio-worker.service /etc/systemd/system/
   install -m 0750 infra/scripts/update-app.sh /usr/local/sbin/quantfolio-update-app
   systemctl daemon-reload
   systemctl enable quantfolio-api quantfolio-worker
   systemctl start quantfolio-api quantfolio-worker
   ```

8. **Verify service:**
   ```bash
   systemctl status quantfolio-api quantfolio-worker
   curl http://<APP_HOST>:8000/health   # the API_HOST you set above
   ```

### Stage 4: Web Container (quantfolio-web, ID 113)

**Note:** The web LXC requires ≥2 GB RAM for the frontend build (Vite + TypeScript compilation). Update the container config in `lxc-web.conf` to allocate 2 GB RAM and 1 GB swap.

1. **SSH into container:**
   ```bash
   pct exec 113 bash
   ```

2. **Install Node.js 20, Caddy, and updater dependencies** (react-router 7 requires Node ≥20; Debian's packaged `nodejs` is too old):
   ```bash
   apt-get update && apt-get install -y git curl caddy
   curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
   apt-get install -y nodejs
   ```
   On an existing deployment still on Node 18, run `infra/scripts/upgrade-node20.sh` instead.

3. **Create quantfolio user and clone the repository (if not already present):**
   ```bash
   useradd -m -s /bin/bash quantfolio
   cd /opt
   git clone https://github.com/JanEric2609/Quantfolio-public.git quantfolio
   cd quantfolio
   chown -R quantfolio:quantfolio /opt/quantfolio
   ```

4. **Build the frontend:**
   ```bash
   cd /opt/quantfolio/frontend
   runuser -u quantfolio -- npm ci
   runuser -u quantfolio -- env NODE_OPTIONS=--max-old-space-size=1536 npm run build
   cd /opt/quantfolio
   ```

5. **Copy Caddyfile and updater:**
   ```bash
   cp /opt/quantfolio/infra/caddy/Caddyfile /etc/caddy/
   install -m 0750 /opt/quantfolio/infra/scripts/update-web.sh /usr/local/sbin/quantfolio-update-web
   ```

6. **For production TLS (self-signed LAN):**
   ```bash
   # Generate self-signed cert
   openssl req -x509 -newkey rsa:4096 -keyout /etc/caddy/quantfolio.key \
     -out /etc/caddy/quantfolio.crt -days 365 -nodes \
     -subj "/CN=quantfolio.local"
   
   # Edit Caddyfile to uncomment self-signed TLS block
   # Uncomment: https://quantfolio.local:443 { ... }
   sed -i 's/^# https:\/\/quantfolio/https:\/\/quantfolio/' /etc/caddy/Caddyfile
   ```

7. **Enable Caddy:**
   ```bash
   caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   systemctl enable caddy
   systemctl start caddy
   systemctl status caddy
   ```

8. **Verify:**
   ```bash
   curl -k https://localhost:443/health
   ```

## Post-Deployment Verification

1. **Run healthcheck from Proxmox host:**
   ```bash
   /root/quantfolio/infra/scripts/healthcheck.sh verbose
   ```

   Should output:
   ```json
   {
     "timestamp": "2026-05-24T12:00:00Z",
     "checks": {
       "database": "ok",
       "redis": "ok",
       "llm_local": "ok",
       "app_api": "ok",
       "llm_anthropic": "ok",  # or "down" if key not configured
       "llm_openai": "ok"
     }
   }
   ```

2. **Test database connection:**
   ```bash
   psql -h <DB_HOST> -U quantfolio_user -d quantfolio -c "SELECT 1;"
   ```

3. **Test LLM inference:**
   ```bash
   curl -X POST http://<LLM_HOST>:8080/v1/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "quantfolio",
       "prompt": "What is 2+2?",
       "max_tokens": 10
     }'
   ```

4. **Test web access:**
   ```bash
   # From workstation on LAN:
   curl -k https://quantfolio-web/  # Should return HTML
   ```

5. **Create test user (in app container):**
   ```bash
   cd /opt/quantfolio/backend
   source .venv/bin/activate
   python -m app.scripts.create_user test@example.com test_password
   ```

## Rollback Procedure

See `rollback.md` for rollback steps.

## Daily Operations

### Scheduled Backup

Set up daily backup (in app container crontab):
```bash
0 2 * * * /opt/quantfolio/infra/scripts/backup.sh /var/backups/quantfolio cron >> /var/log/quantfolio-backup.log 2>&1
```

### Unattended Updates (SSH deploy keys)

The updater fetches over `origin`. With the default HTTPS remote, `quantfolio-update` prompts for GitHub credentials on every fetch/pull, so it cannot run from a timer. To enable hands-off updates, switch the App (111) and Web (113) checkouts to SSH with a **read-only** deploy key:

```bash
# Inside each of LXC 111 and 113 (run as root):
sh /opt/quantfolio/infra/scripts/setup-git-ssh.sh
```

The script generates an ed25519 key for the `quantfolio` user, trusts `github.com`, repoints `origin` to SSH, and prints the public key. Add each printed key under **GitHub → repo → Settings → Deploy keys** with **Allow write access unchecked** (the updater only fetches). Deploy keys are unique per repo, so each LXC needs its own. After both are added, `quantfolio-update` runs without prompting.

### Monitor Logs

```bash
# App logs
pct exec 111 journalctl -u quantfolio-api -f

# LLM logs
pct exec 112 journalctl -u quantfolio-llamacpp -f

# Caddy logs
pct exec 113 journalctl -u caddy -f
```

### Update Cloud API Keys

```bash
# In app container, update Settings → Integrations
# Keys are encrypted via SecretBox (in api_keys table)
```

## Incident Response

- **LLM down**: See `incident-llm-down.md`
- **Cloud keys rotated**: See `incident-cloud-keys-rotated.md`
- **Database crash**: See backup-restore procedure in `backup-restore.md`

---

**Last updated:** 2026-05-24  
**Phase:** 9  
**Status:** Ready for deployment
