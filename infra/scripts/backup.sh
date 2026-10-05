#!/bin/bash
# QuantFolio Backup Script
# Backs up: PostgreSQL dump, Obsidian vault, encrypted API keys
# Usage: ./backup.sh [backup_dir] [schedule]
# Default: ./backup.sh /var/backups/quantfolio cron

set -euo pipefail

# Configuration
BACKUP_DIR="${1:-/var/backups/quantfolio}"
SCHEDULE="${2:-cron}"  # cron | manual | test
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/quantfolio-backup-$TIMESTAMP.tar.gz"
DB_BACKUP="$BACKUP_DIR/db-$TIMESTAMP.sql"
VAULT_BACKUP="$BACKUP_DIR/vault-$TIMESTAMP.tar.gz"
KEYS_BACKUP="$BACKUP_DIR/api_keys-$TIMESTAMP.json.enc"

# Paths
OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-/mnt/c/Obsidian\ Vault/LLM}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-quantfolio}"
DB_USER="${DB_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"  # From env or .env

# Create backup directory
mkdir -p "$BACKUP_DIR"

echo "[$SCHEDULE] Starting QuantFolio backup at $(date)"
echo "  Backup directory: $BACKUP_DIR"
echo "  Database: $DB_HOST:$DB_PORT/$DB_NAME"
echo "  Vault: $OBSIDIAN_VAULT"

# ====================================================================
# 1. Database Backup (pg_dump)
# ====================================================================

echo "[$(date +%H:%M:%S)] Backing up PostgreSQL database..."

if [ -n "$POSTGRES_PASSWORD" ]; then
    PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --verbose \
        --format=plain \
        --no-password \
        "$DB_NAME" > "$DB_BACKUP" 2>&1
else
    pg_dump \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --verbose \
        --format=plain \
        "$DB_NAME" > "$DB_BACKUP"
fi

DB_SIZE=$(du -h "$DB_BACKUP" | cut -f1)
echo "  Database backed up: $DB_BACKUP ($DB_SIZE)"

# ====================================================================
# 2. Obsidian Vault Backup
# ====================================================================

echo "[$(date +%H:%M:%S)] Backing up Obsidian vault..."

if [ -d "$OBSIDIAN_VAULT" ]; then
    tar --exclude='.Trash*' \
        --exclude='.obsidian/cache' \
        --exclude='.obsidian/plugins' \
        -czf "$VAULT_BACKUP" \
        -C "$(dirname "$OBSIDIAN_VAULT")" \
        "$(basename "$OBSIDIAN_VAULT")" \
        2>&1 | grep -v "tar:" || true

    VAULT_SIZE=$(du -h "$VAULT_BACKUP" | cut -f1)
    echo "  Vault backed up: $VAULT_BACKUP ($VAULT_SIZE)"
else
    echo "  WARNING: Vault directory not found at $OBSIDIAN_VAULT (skipped)"
fi

# ====================================================================
# 3. Encrypted API Keys Backup
# ====================================================================

echo "[$(date +%H:%M:%S)] Backing up encrypted API keys..."

# Extract api_keys table (encrypted) from database.
# Column name is `key_encrypted` (see backend/app/models/entities.py:ApiKey).
PGPASSWORD="$POSTGRES_PASSWORD" psql \
    --host="$DB_HOST" \
    --port="$DB_PORT" \
    --username="$DB_USER" \
    --no-password \
    --dbname="$DB_NAME" \
    --tuples-only \
    --command="SELECT service, key_encrypted, meta_json FROM api_keys ORDER BY id;" \
    | gzip > "$KEYS_BACKUP.gz"

KEYS_SIZE=$(du -h "$KEYS_BACKUP.gz" | cut -f1)
echo "  API keys backed up (encrypted): $KEYS_BACKUP.gz ($KEYS_SIZE)"

# ====================================================================
# 4. Create Manifest
# ====================================================================

MANIFEST="$BACKUP_DIR/manifest-$TIMESTAMP.json"
cat > "$MANIFEST" << EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "schedule": "$SCHEDULE",
  "hostname": "$(hostname)",
  "app_version": "0.1.0",
  "files": {
    "database": "$(basename $DB_BACKUP)",
    "vault": "$(basename $VAULT_BACKUP)",
    "api_keys": "$(basename $KEYS_BACKUP).gz"
  },
  "checksums": {
    "database_md5": "$(md5sum $DB_BACKUP | cut -d' ' -f1)",
    "vault_md5": "$(md5sum $VAULT_BACKUP | cut -d' ' -f1)",
    "api_keys_md5": "$(md5sum $KEYS_BACKUP.gz | cut -d' ' -f1)"
  },
  "notes": "API keys are encrypted at rest. Database is plaintext SQL."
}
EOF

echo "  Manifest: $MANIFEST"

# ====================================================================
# 5. Retention Policy (keep last 30 backups)
# ====================================================================

echo "[$(date +%H:%M:%S)] Applying retention policy (keep last 30 backups)..."

ls -t "$BACKUP_DIR"/quantfolio-backup-*.tar.gz 2>/dev/null | tail -n +31 | xargs -r rm -v || true
ls -t "$BACKUP_DIR"/db-*.sql 2>/dev/null | tail -n +31 | xargs -r rm -v || true
ls -t "$BACKUP_DIR"/manifest-*.json 2>/dev/null | tail -n +31 | xargs -r rm -v || true

# ====================================================================
# 6. Summary
# ====================================================================

TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
echo ""
echo "[$SCHEDULE] Backup completed successfully at $(date)"
echo "  Total backup size: $TOTAL_SIZE"
echo "  Manifest: $MANIFEST"
echo ""
echo "To restore, run: ./restore.sh $MANIFEST"

# Optional: send to remote storage (e.g., rsync to NAS)
# If configured via env var:
if [ -n "${BACKUP_REMOTE:-}" ]; then
    echo "[$(date +%H:%M:%S)] Uploading to remote storage: $BACKUP_REMOTE"
    rsync -avz "$BACKUP_FILE" "$BACKUP_REMOTE/" || echo "WARNING: Remote backup failed"
fi

exit 0
