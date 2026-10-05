#!/bin/bash
# QuantFolio Restore Script
# Restores from backup created by backup.sh
# Usage: ./restore.sh <manifest_json> [target_db_name]

set -euo pipefail

# Validate arguments
if [ $# -lt 1 ]; then
    echo "Usage: $0 <manifest_json> [target_db_name]"
    echo "  Example: $0 /var/backups/quantfolio/manifest-20260524_120000.json"
    exit 1
fi

MANIFEST="$1"
TARGET_DB="${2:-quantfolio_restored}"

if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: Manifest file not found: $MANIFEST"
    exit 1
fi

# Parse manifest
BACKUP_DIR=$(dirname "$MANIFEST")
DB_BACKUP=$(jq -r '.files.database' "$MANIFEST")
VAULT_BACKUP=$(jq -r '.files.vault' "$MANIFEST")
TIMESTAMP=$(jq -r '.timestamp' "$MANIFEST")

DB_FILE="$BACKUP_DIR/$DB_BACKUP"
VAULT_FILE="$BACKUP_DIR/$VAULT_BACKUP"

# Database credentials
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"

OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-/mnt/c/Obsidian\ Vault/LLM}"

echo "=========================================="
echo "QuantFolio Restore from Backup"
echo "=========================================="
echo "Manifest: $MANIFEST"
echo "Backup timestamp: $TIMESTAMP"
echo "Target database: $TARGET_DB"
echo "Vault restore path: $OBSIDIAN_VAULT"
echo ""

# ====================================================================
# 1. Restore Database
# ====================================================================

echo "[$(date +%H:%M:%S)] Restoring database..."

if [ ! -f "$DB_FILE" ]; then
    echo "ERROR: Database backup file not found: $DB_FILE"
    exit 1
fi

# Create target database (drop if exists)
if [ -n "$POSTGRES_PASSWORD" ]; then
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --no-password \
        --command="DROP DATABASE IF EXISTS $TARGET_DB;"
else
    psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --command="DROP DATABASE IF EXISTS $TARGET_DB;"
fi

# Create fresh database
if [ -n "$POSTGRES_PASSWORD" ]; then
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --no-password \
        --command="CREATE DATABASE $TARGET_DB OWNER $DB_USER;"
else
    psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --command="CREATE DATABASE $TARGET_DB OWNER $DB_USER;"
fi

# Restore SQL dump
if [ -n "$POSTGRES_PASSWORD" ]; then
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --no-password \
        --dbname="$TARGET_DB" \
        < "$DB_FILE"
else
    psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --dbname="$TARGET_DB" \
        < "$DB_FILE"
fi

echo "  Database restored: $TARGET_DB"

# ====================================================================
# 2. Restore Obsidian Vault
# ====================================================================

echo "[$(date +%H:%M:%S)] Restoring Obsidian vault..."

if [ ! -f "$VAULT_FILE" ]; then
    echo "  WARNING: Vault backup file not found (skipped): $VAULT_FILE"
else
    # Create vault parent directory if needed
    VAULT_PARENT=$(dirname "$OBSIDIAN_VAULT")
    mkdir -p "$VAULT_PARENT"

    # Backup existing vault if present
    if [ -d "$OBSIDIAN_VAULT" ]; then
        VAULT_BACKUP_DIR="$VAULT_PARENT/LLM-backup-$(date +%Y%m%d_%H%M%S)"
        mv "$OBSIDIAN_VAULT" "$VAULT_BACKUP_DIR"
        echo "  Existing vault moved to: $VAULT_BACKUP_DIR"
    fi

    # Extract vault
    tar -xzf "$VAULT_FILE" -C "$VAULT_PARENT"
    echo "  Vault restored: $OBSIDIAN_VAULT"
fi

# ====================================================================
# 3. Restore Encrypted API Keys
# ====================================================================

echo "[$(date +%H:%M:%S)] Restoring encrypted API keys..."

KEYS_BACKUP=$(jq -r '.files.api_keys' "$MANIFEST")
KEYS_FILE="$BACKUP_DIR/$KEYS_BACKUP.gz"

if [ ! -f "$KEYS_FILE" ]; then
    echo "  WARNING: API keys backup file not found (skipped): $KEYS_FILE"
else
    # Keys are already encrypted in database, just verify counts
    echo "  Note: API keys are restored within the database dump above"
    echo "  Backup file (for reference): $KEYS_FILE"
fi

# ====================================================================
# 4. Post-Restore Validation
# ====================================================================

echo "[$(date +%H:%M:%S)] Validating restore..."

# Test database connection
if [ -n "$POSTGRES_PASSWORD" ]; then
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --no-password \
        --dbname="$TARGET_DB" \
        --command="SELECT COUNT(*) as table_count FROM information_schema.tables WHERE table_schema='public';" || exit 1
else
    psql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --dbname="$TARGET_DB" \
        --command="SELECT COUNT(*) as table_count FROM information_schema.tables WHERE table_schema='public';" || exit 1
fi

TABLE_COUNT=$(psql \
    --host="$DB_HOST" \
    --port="$DB_PORT" \
    --username="$DB_USER" \
    --dbname="$TARGET_DB" \
    --tuples-only \
    --command="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';")

echo "  Database validation: $TABLE_COUNT tables found"

# ====================================================================
# 5. Summary
# ====================================================================

echo ""
echo "=========================================="
echo "Restore completed successfully at $(date)"
echo "=========================================="
echo "Target database: $TARGET_DB"
echo "Tables restored: $TABLE_COUNT"
echo "Vault restored: $OBSIDIAN_VAULT"
echo ""
echo "Next steps:"
echo "  1. If this is production: back up current 'quantfolio' database"
echo "  2. Rename '$TARGET_DB' to 'quantfolio'"
echo "  3. Restart the application: systemctl restart quantfolio-api"
echo ""

exit 0
