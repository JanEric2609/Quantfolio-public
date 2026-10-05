# QuantFolio Backup and Restore Runbook

## Backup Strategy

**Schedule:** Daily at 2 AM (configured via cron)  
**Retention:** Last 30 backups (~90 days at daily frequency)  
**Components:** Database (pg_dump), Obsidian vault, Encrypted API keys  
**Backup size:** ~15-20 GB per backup (compressed)  

## Creating a Manual Backup

```bash
# In app container (111)
cd /opt/quantfolio
./infra/scripts/backup.sh /var/backups/quantfolio manual

# Or from Proxmox host (mounts backup dir)
/root/quantfolio/infra/scripts/backup.sh /var/backups/quantfolio manual
```

**Output:**
```
Manifest: /var/backups/quantfolio/manifest-20260524_120000.json
Database: /var/backups/quantfolio/db-20260524_120000.sql
Vault: /var/backups/quantfolio/vault-20260524_120000.tar.gz
API Keys: /var/backups/quantfolio/api_keys-20260524_120000.json.enc.gz
```

## Listing Available Backups

```bash
# On Proxmox host or app container
ls -lh /var/backups/quantfolio/manifest-*.json

# Show creation dates
ls -lt /var/backups/quantfolio/ | grep manifest
```

## Restoring from Backup

### 1. Verify Backup Integrity

```bash
# Check manifest exists
cat /var/backups/quantfolio/manifest-20260524_120000.json | jq '.'

# Verify all files present
jq -r '.files | .[]' /var/backups/quantfolio/manifest-20260524_120000.json | \
  while read f; do [ -f "/var/backups/quantfolio/$f" ] && echo "✓ $f" || echo "✗ MISSING: $f"; done
```

### 2. Full System Restore (Production)

**WARNING:** This procedure restores the entire database. Production data will be overwritten.

```bash
# Step 1: Stop application
pct exec 111 systemctl stop quantfolio-api quantfolio-worker

# Step 2: Stop database
pct exec 110 systemctl stop postgresql

# Step 3: Restore from backup
./infra/scripts/restore.sh /var/backups/quantfolio/manifest-20260524_120000.json

# Step 4: Start database
pct exec 110 systemctl start postgresql

# Step 5: Verify restored data
pct exec 110 psql -h localhost -U postgres -d quantfolio_restored -c "SELECT COUNT(*) FROM portfolios;"

# Step 6: Rename restored database
pct exec 110 psql -h localhost -U postgres << 'EOF'
ALTER DATABASE quantfolio RENAME TO quantfolio_backup_old;
ALTER DATABASE quantfolio_restored RENAME TO quantfolio;
EOF

# Step 7: Start application
pct exec 111 systemctl start quantfolio-api quantfolio-worker

# Step 8: Verify
curl http://<APP_HOST>:8000/health
```

### 3. Restore to Test Environment

To test a restore without affecting production:

```bash
# Create test database
pct exec 110 psql -h localhost -U postgres << 'EOF'
CREATE DATABASE quantfolio_test;
EOF

# Restore into test DB
./infra/scripts/restore.sh /var/backups/quantfolio/manifest-20260524_120000.json quantfolio_test

# Connect and verify
pct exec 110 psql -h localhost -U postgres -d quantfolio_test -c "SELECT COUNT(*) FROM portfolios;"

# Cleanup when done
pct exec 110 psql -h localhost -U postgres -c "DROP DATABASE quantfolio_test;"
```

## Point-in-Time Recovery (PITR)

PostgreSQL WAL (Write-Ahead Logs) enable recovery to any point in time. To set up:

```bash
# On database container (110)
# Edit /etc/postgresql/16/main/postgresql.conf:

wal_level = replica
archive_mode = on
archive_command = 'test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f'

# Create archive directory
mkdir -p /var/lib/postgresql/wal_archive
chown postgres:postgres /var/lib/postgresql/wal_archive

# Restart PostgreSQL
systemctl restart postgresql

# To restore to a point in time:
# 1. Obtain last base backup: /var/backups/quantfolio/db-*.sql
# 2. Restore base backup to recovery_target_timeline='latest'
# See PostgreSQL PITR docs for full procedure
```

## Automated Backup Scheduling

Add to app container crontab:

```bash
# In app container (111)
crontab -e

# Add line:
0 2 * * * /opt/quantfolio/infra/scripts/backup.sh /var/backups/quantfolio cron >> /var/log/quantfolio-backup.log 2>&1

# Verify
crontab -l
```

Or use systemd timer (preferred):

```bash
# Create /etc/systemd/system/quantfolio-backup.service
[Unit]
Description=QuantFolio Daily Backup
After=quantfolio-api.service

[Service]
Type=oneshot
ExecStart=/opt/quantfolio/infra/scripts/backup.sh /var/backups/quantfolio cron
User=quantfolio

# Create /etc/systemd/system/quantfolio-backup.timer
[Unit]
Description=QuantFolio Daily Backup Timer
Requires=quantfolio-backup.service

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target

# Enable
systemctl daemon-reload
systemctl enable --now quantfolio-backup.timer
systemctl status quantfolio-backup.timer
```

## Remote Backup Storage

To back up to a NAS or cloud storage:

```bash
# Set env var before running backup
export BACKUP_REMOTE=backup_user@<BACKUP_HOST>:/nas/quantfolio_backups

# Run backup (will rsync to remote after local backup)
./backup.sh /var/backups/quantfolio

# Or manually sync existing backups
rsync -avz /var/backups/quantfolio/ backup_user@<BACKUP_HOST>:/nas/quantfolio_backups/
```

## Backup Integrity Checks

### Database Consistency

```bash
# Verify database integrity
pct exec 110 psql -h localhost -U quantfolio_user -d quantfolio << 'EOF'
-- Check for missing foreign keys
SELECT * FROM information_schema.table_constraints WHERE constraint_type='FOREIGN KEY';

-- Check table sizes
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename))
FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
EOF
```

### Backup File Integrity

```bash
# Verify checksums match manifest
jq -r '.checksums | to_entries[] | "\(.value) /var/backups/quantfolio/\(.key | gsub("_md5"; ""))"' \
  /var/backups/quantfolio/manifest-*.json | \
  md5sum -c
```

## Disaster Recovery Timeframes

| Scenario | RTO | RPO |
|----------|-----|-----|
| Single container failure | 5 min | 24 hours |
| Datacenter failure (restore to new host) | 1-2 hours | 24 hours |
| Ransomware/data corruption | 30 min | 24 hours |

**RTO:** Recovery Time Objective (how fast you can restore)  
**RPO:** Recovery Point Objective (how much data loss is acceptable)

Current setup: RPO = 24 hours (daily backups). For RPO < 1 hour, implement WAL archiving (see PITR section).

---

**Last updated:** 2026-05-24  
**Related:** `deploy.md`, `rollback.md`
