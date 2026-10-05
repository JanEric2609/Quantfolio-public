# QuantFolio Rollback Runbook

**When to rollback:** Critical issues (boot loop, data corruption, unreachable after deploy).

## Full Rollback (Revert to Previous Snapshot)

If Proxmox snapshots were taken before deployment:

```bash
# On Proxmox host
lvconvert --merge /dev/pve/root-backup-snapshot
# Reboot host
reboot
```

## Per-LXC Rollback

QuantFolio updates are now applied per-LXC. Rollback is therefore specific to the container(s) affected by the change.

### App LXC (111) Rollback

If the backend or migration is broken:

```bash
# In app container (111)
cd /opt/quantfolio
runuser -u quantfolio -- git log --oneline -5
runuser -u quantfolio -- git checkout <previous-commit-hash>

runuser -u quantfolio -- backend/.venv/bin/alembic downgrade -1  # Downgrade one migration
# Or downgrade to specific revision
runuser -u quantfolio -- backend/.venv/bin/alembic downgrade <revision>

systemctl restart quantfolio-api quantfolio-worker
curl http://<APP_HOST>:8000/health/ready   # your API_HOST (127.0.0.1 on a single node)
```

The pre-update database backup is available in `/var/backups/quantfolio/` if you need to restore from it.

### Web LXC (113) Rollback

If the frontend build or deployment is broken:

```bash
# In web container (113)
cd /opt/quantfolio
runuser -u quantfolio -- git log --oneline -5
runuser -u quantfolio -- git checkout <previous-commit-hash>

cd frontend
runuser -u quantfolio -- npm ci
runuser -u quantfolio -- env NODE_OPTIONS=--max-old-space-size=1536 npm run build
cd ..

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload-or-restart caddy
curl http://127.0.0.1/health
```

## Container-Level Rollback

If a single container is corrupted:

### 1. Stop the Container

```bash
# On Proxmox host
pct stop <vmid>  # e.g., pct stop 111
```

### 2. Restore from Backup

```bash
# Option A: Restore from LVM snapshot (if available)
lvconvert --merge /dev/pve/vm-111-disk-0-backup

# Option B: Restore from backup script
/root/quantfolio/infra/scripts/restore.sh /var/backups/quantfolio/manifest-*.json
```

### 3. Start Container

```bash
pct start <vmid>
```

## Full System Rollback

If multiple components are affected:

### 1. Stop All Services

```bash
# On Proxmox host
pct stop 110 111 112 113
```

### 2. Restore All Containers from Backup

```bash
./infra/scripts/restore.sh /var/backups/quantfolio/manifest-<TIMESTAMP>.json
```

### 3. Verify Backup Integrity

```bash
psql -h <DB_HOST> -U quantfolio_user -d quantfolio -c "SELECT COUNT(*) FROM portfolios;"
```

### 4. Start Containers in Order

```bash
pct start 110  # DB
sleep 10
pct start 112  # LLM
pct start 111  # App
pct start 113  # Web
```

### 5. Run Healthcheck

```bash
./infra/scripts/healthcheck.sh verbose
```

## Incident Communication

1. **Alert team:** "Incident: [service] down, initiating rollback"
2. **Estimate downtime:** ~5-10 minutes for container restart, ~30 min for full restore
3. **Post-mortem:** Document root cause after recovery

## Prevention

- **Take snapshots before major updates:**
  ```bash
  lvcreate -L10G -s -n root-backup-snapshot /dev/pve/root
  lvcreate -L50G -s -n vm-111-backup-snapshot /dev/pve/vm-111-disk-0
  ```

- **Automated backups:** Cron job runs `backup.sh` daily at 2 AM
- **Test restores monthly:** `restore.sh` on a non-production DB to verify integrity

---

**Reference:** Phase 9 deployment  
**Last updated:** 2026-05-24
