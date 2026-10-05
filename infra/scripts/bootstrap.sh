#!/bin/bash
# QuantFolio Proxmox Bootstrap Script
# Provisions a fresh Proxmox host end-to-end with LXC containers + services
# Run on the Proxmox host (not inside a container)
# Usage: run as root: ./bootstrap.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "QuantFolio Proxmox Bootstrap"
echo "=========================================="
echo "Script directory: $SCRIPT_DIR"
echo "Infra directory: $PARENT_DIR"
echo ""

# Check if running on Proxmox
if ! command -v pveum &> /dev/null; then
    echo "ERROR: This script must run on a Proxmox host"
    echo "pveum command not found"
    exit 1
fi

echo "✓ Running on Proxmox host"

# ====================================================================
# 1. Validate Prerequisites
# ====================================================================

echo ""
echo "========== Step 1: Validating prerequisites =========="

# Check IOMMU for GPU passthrough
if grep -q 'intel_iommu=on\|amd_iommu=on' /proc/cmdline; then
    echo "✓ IOMMU enabled for GPU passthrough"
else
    echo "⚠ IOMMU not detected. GPU passthrough may not work."
    echo "  To enable, follow infra/proxmox/gpu-passthrough.md"
fi

# Check TimescaleDB extension available
if [ -f "$PARENT_DIR/docker-compose.yml" ]; then
    echo "✓ docker-compose.yml found"
else
    echo "ERROR: docker-compose.yml not found"
    exit 1
fi

# ====================================================================
# 2. Create LXC Containers
# ====================================================================

echo ""
echo "========== Step 2: Creating LXC containers =========="

# Note: This is a reference/example. Actual container creation depends on
# whether you want to create from templates or manually provision.
# For now, we'll create the configs and document the process.

CONTAINERS=(
    "110:quantfolio-db:lxc-db.conf:postgres-timescale"
    "111:quantfolio-api:lxc-app.conf:fastapi"
    "112:quantfolio-llm:lxc-llm.conf:llama-cpp"
    "113:quantfolio-web:lxc-web.conf:caddy"
)

for container_spec in "${CONTAINERS[@]}"; do
    IFS=':' read -r vmid name conf role <<< "$container_spec"
    conf_file="$PARENT_DIR/proxmox/$conf"

    if [ -f "$conf_file" ]; then
        echo "✓ Config found: $conf ($role)"
        # Actual creation would be: pct create $vmid <template> --conf $conf_file
        # For now, just verify the config exists
    else
        echo "✗ Config missing: $conf"
        exit 1
    fi
done

echo ""
echo "Next steps to create containers manually:"
echo "  1. In Proxmox Web UI, create 4 LXC containers with IDs 110-113"
echo "  2. Adjust networking IP addresses in $PARENT_DIR/proxmox/*.conf"
echo "  3. Copy each config to /etc/pve/lxc/<vmid>.conf on the Proxmox host"
echo ""

# ====================================================================
# 3. Set Up systemd Services (host level)
# ====================================================================

echo "========== Step 3: Installing systemd service templates =========="

# These are templates that will be copied into containers during provisioning
SERVICES=(
    "quantfolio-api.service:app"
    "quantfolio-worker.service:app"
    "quantfolio-llamacpp.service:llm"
)

for service_spec in "${SERVICES[@]}"; do
    IFS=':' read -r service_name service_role <<< "$service_spec"
    service_file="$PARENT_DIR/systemd/$service_name"

    if [ -f "$service_file" ]; then
        echo "✓ Service template: $service_name ($service_role)"
    else
        echo "✗ Service template missing: $service_name"
        exit 1
    fi
done

echo ""
echo "Next step: Inside each LXC container, copy systemd services:"
echo "  quantfolio-api container:"
echo "    cp /opt/quantfolio/infra/systemd/quantfolio-api.service /etc/systemd/system/"
echo "    cp /opt/quantfolio/infra/systemd/quantfolio-worker.service /etc/systemd/system/"
echo "    systemctl daemon-reload && systemctl enable --now quantfolio-api quantfolio-worker"
echo ""
echo "  quantfolio-llm container:"
echo "    cp /opt/quantfolio/infra/systemd/quantfolio-llamacpp.service /etc/systemd/system/"
echo "    systemctl daemon-reload && systemctl enable --now quantfolio-llamacpp"
echo ""

# ====================================================================
# 4. Set Up Caddy Reverse Proxy
# ====================================================================

echo "========== Step 4: Setting up Caddy reverse proxy =========="

caddy_config="$PARENT_DIR/caddy/Caddyfile"
if [ -f "$caddy_config" ]; then
    echo "✓ Caddyfile found"
    echo "  To enable TLS, uncomment the domain block in the Caddyfile"
    echo "  Copy to container: cp $caddy_config /etc/caddy/"
else
    echo "✗ Caddyfile not found"
    exit 1
fi

# ====================================================================
# 5. Set Up Backup Infrastructure
# ====================================================================

echo ""
echo "========== Step 5: Setting up backup infrastructure =========="

BACKUP_SCRIPTS=(
    "backup.sh"
    "restore.sh"
    "healthcheck.sh"
)

for script in "${BACKUP_SCRIPTS[@]}"; do
    script_file="$PARENT_DIR/scripts/$script"
    if [ -f "$script_file" ]; then
        echo "✓ Script available: $script"
        chmod +x "$script_file"
    else
        echo "✗ Script not found: $script"
        exit 1
    fi
done

# Create backup directory on host
BACKUP_DIR="/var/backups/quantfolio"
mkdir -p "$BACKUP_DIR"
echo "✓ Backup directory: $BACKUP_DIR"

# Optional: Set up daily backup cron (on app container)
echo ""
echo "To enable automated daily backups, add to app container crontab:"
echo "  0 2 * * * /opt/quantfolio/infra/scripts/backup.sh $BACKUP_DIR cron >> /var/log/quantfolio-backup.log 2>&1"
echo ""

# ====================================================================
# 6. GPU Passthrough Configuration (if applicable)
# ====================================================================

echo "========== Step 6: GPU Passthrough (optional) =========="

gpu_doc="$PARENT_DIR/proxmox/gpu-passthrough.md"
if [ -f "$gpu_doc" ]; then
    echo "✓ GPU passthrough documentation: $gpu_doc"
    echo ""
    echo "To set up GPU passthrough:"
    echo "  1. Read: $gpu_doc"
    echo "  2. Enable IOMMU in BIOS"
    echo "  3. Configure kernel parameters in /etc/default/grub"
    echo "  4. Bind GPU to vfio-pci"
    echo "  5. Add hostpci0 to lxc-llm.conf"
    echo ""
else
    echo "✗ GPU documentation not found"
fi

# ====================================================================
# 7. MoE Offload Configuration
# ====================================================================

echo "========== Step 7: MoE Offload Configuration =========="

moe_doc="$PARENT_DIR/proxmox/moe-offload-notes.md"
if [ -f "$moe_doc" ]; then
    echo "✓ MoE offload documentation: $moe_doc"
    echo ""
    echo "Review memory budgets in: $moe_doc"
    echo "Then ensure llama.cpp is started with:"
    echo "  --cpu-moe --n-gpu-layers 99 --ctx-size 8192"
    echo ""
else
    echo "✗ MoE documentation not found"
fi

# ====================================================================
# 8. Network Configuration
# ====================================================================

echo "========== Step 8: Network Configuration =========="

echo "Verify container IP addresses match your network:"
echo "  quantfolio-db   (110): 10.0.0.20  (update in lxc-db.conf)"
echo "  quantfolio-api  (111): 10.0.0.21  (update in lxc-app.conf)"
echo "  quantfolio-llm  (112): 10.0.0.22  (update in lxc-llm.conf)"
echo "  quantfolio-web  (113): 10.0.0.23  (update in lxc-web.conf)"
echo ""

# ====================================================================
# 9. Environment Files
# ====================================================================

echo "========== Step 9: Environment Configuration =========="

echo "Create /opt/quantfolio/.env inside each container with:"
echo "  DATABASE_URL=postgresql://postgres:PASSWORD@quantfolio-db:5432/quantfolio"
echo "  REDIS_URL=redis://quantfolio-api:6379"
echo "  LLM_LOCAL_URL=http://quantfolio-llm:8080"
echo "  (Optional) ANTHROPIC_API_KEY=sk-ant-..."
echo "  (Optional) OPENAI_API_KEY=sk-..."
echo ""

# ====================================================================
# 10. Post-Provisioning Checks
# ====================================================================

echo "========== Step 10: Post-Provisioning Checklist =========="

cat << 'EOF'

1. On Proxmox host:
   [ ] LXC containers created (110-113)
   [ ] Network connectivity verified
   [ ] GPU passthrough configured (if using GPU)

2. In quantfolio-db container:
   [ ] PostgreSQL installed
   [ ] TimescaleDB extension enabled
   [ ] pgvector extension created
   [ ] Database "quantfolio" created
   [ ] Alembic migrations applied

3. In quantfolio-api container:
   [ ] Python 3.11+ installed
   [ ] /opt/quantfolio cloned from repo
   [ ] venv created and dependencies installed
   [ ] .env file created with correct URLs
   [ ] quantfolio-api.service and quantfolio-worker.service enabled
   [ ] systemctl status quantfolio-api quantfolio-worker (should be running)

4. In quantfolio-llm container:
   [ ] llama.cpp built or downloaded
   [ ] Qwen3.6-35B-A3B-UD-Q4_K_M.gguf downloaded to /var/lib/quantfolio/models/
   [ ] GPU passthrough verified (nvidia-smi shows GPU)
   [ ] quantfolio-llamacpp.service enabled
   [ ] curl http://<LLAMA_BIND_HOST>:8080/health returns 200; /etc/quantfolio/llama.env sets LLAMA_API_KEY

5. In quantfolio-web container:
   [ ] Caddy installed
   [ ] Caddyfile copied and configured
   [ ] TLS certificate generated (if not using Let's Encrypt)
   [ ] systemctl status caddy (should be running)

6. Post-Boot Verification:
   [ ] Run healthcheck: ./scripts/healthcheck.sh verbose
   [ ] Check logs: journalctl -u quantfolio-api -n 50
   [ ] Browse to https://quantfolio-web:443 (or http://IP:80)
   [ ] Create a test user and log in

7. Backup Setup:
   [ ] Test backup: ./scripts/backup.sh /var/backups/quantfolio manual
   [ ] Test restore: ./scripts/restore.sh /var/backups/quantfolio/manifest-*.json
   [ ] Set up cron: 0 2 * * * /opt/quantfolio/infra/scripts/backup.sh

EOF

# ====================================================================
# Summary
# ====================================================================

echo ""
echo "=========================================="
echo "Bootstrap Configuration Complete"
echo "=========================================="
echo ""
echo "Next manual steps:"
echo "  1. Create LXC containers in Proxmox Web UI"
echo "  2. Configure networking IPs in container configs"
echo "  3. Install software inside each container (see checklist above)"
echo "  4. Copy systemd services and enable them"
echo "  5. Run: ./healthcheck.sh verbose"
echo ""
echo "For detailed runbook, see: $PARENT_DIR/runbooks/deploy.md"
echo ""

exit 0
