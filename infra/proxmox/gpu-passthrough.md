# Proxmox GPU Passthrough Configuration

This guide enables GPU passthrough for the `quantfolio-llm` LXC container running llama.cpp with Qwen3.6-35B-A3B-UD-Q4_K_M.

## Prerequisites

- Proxmox VE 8.0+
- Intel or AMD CPU with IOMMU support enabled in BIOS
- NVIDIA GPU (this guide uses RTX 3060 8GB as example)
- SSH access to the Proxmox host

## Step 1: Enable IOMMU in BIOS

### Intel Processors
1. Reboot Proxmox host into BIOS
2. Navigate to `Advanced → System Agent Configuration → VT-d`
3. Enable VT-d
4. Save and exit

### AMD Processors
1. Reboot Proxmox host into BIOS
2. Navigate to `Advanced → AMD CBS → IOMMU`
3. Enable IOMMU
4. Save and exit

## Step 2: Configure Proxmox Kernel Parameters

SSH into the Proxmox host and edit `/etc/default/grub`:

```bash
vim /etc/default/grub
```

Find the `GRUB_CMDLINE_LINUX_DEFAULT` line and ensure it contains:
- **Intel**: `intel_iommu=on iommu=pt`
- **AMD**: `amd_iommu=on iommu=pt`

Example (Intel):
```
GRUB_CMDLINE_LINUX_DEFAULT="quiet intel_iommu=on iommu=pt"
```

Save and update GRUB:
```bash
update-grub
reboot
```

## Step 3: Load Required Kernel Modules

After reboot, add modules to `/etc/modules-load.d/pve-blacklist.conf`:

```bash
echo "vfio" >> /etc/modules-load.d/pve-blacklist.conf
echo "vfio_iommu_type1" >> /etc/modules-load.d/pve-blacklist.conf
echo "vfio_virqfd" >> /etc/modules-load.d/pve-blacklist.conf
```

If using NVIDIA:
```bash
echo "vfio-pci" >> /etc/modules-load.d/pve-blacklist.conf
```

Reload modules:
```bash
modprobe vfio
modprobe vfio_iommu_type1
modprobe vfio_virqfd
modprobe vfio-pci
```

## Step 4: Identify and Bind GPU to VFIO

List PCI devices to find your GPU:
```bash
lspci | grep -i nvidia
# Example output: 01:00.0 VGA compatible controller: NVIDIA Corporation GA106 [GeForce RTX 3060]
```

Note the PCI ID (e.g., `01:00.0`).

Get the IOMMU group:
```bash
find /sys/kernel/iommu_groups/ -type l | grep 01:00 | head -1
# Should show something like: /sys/kernel/iommu_groups/13/devices/0000:01:00.0
```

Bind the GPU to `vfio-pci`. Edit `/etc/modprobe.d/vfio.conf`:

```bash
echo "options vfio-pci ids=10de:2504,10de:228e" >> /etc/modprobe.d/vfio.conf
# Replace 10de:2504 and 10de:228e with your GPU's vendor:device codes (from lspci -nn)
```

Get the vendor and device codes:
```bash
lspci -nn | grep -i nvidia
# Example: 01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA106 [GeForce RTX 3060] [10de:2504] (rev a1)
```

Blacklist NVIDIA drivers (if installed):
```bash
echo "blacklist nouveau" >> /etc/modprobe.d/blacklist.conf
echo "blacklist nvidia" >> /etc/modprobe.d/blacklist.conf
```

Regenerate initramfs:
```bash
update-initramfs -u -k all
reboot
```

## Step 5: Verify GPU Binding

After reboot, verify the GPU is bound to VFIO:
```bash
lspci -vvv -s 01:00.0
# Look for "Kernel driver in use: vfio-pci"
```

## Step 6: Add GPU to LXC Container

Edit the LXC container config (e.g., `/etc/pve/lxc/112.conf`) and add:

```
hostpci0: 01:00.0,pcie=1
```

The container will now have access to the GPU.

## Step 7: Verify GPU Access in Container

Start the container and access its console:
```bash
pct start 112
pct console 112
```

Inside the container, check GPU visibility:
```bash
lspci | grep -i nvidia
# Should show the GPU
```

Install NVIDIA drivers (if needed for non-llama.cpp workloads):
```bash
apt-get install nvidia-driver-550
```

## Step 8: Configure llama.cpp Service

The `quantfolio-llamacpp.service` systemd unit should have `--n-gpu-layers 99` set. Verify:
```bash
systemctl cat quantfolio-llamacpp.service | grep -A 5 "ExecStart"
```

Should show:
```
--n-gpu-layers 99 \
--gpu-type cuda \  # or rocm for AMD
```

## Troubleshooting

### GPU Not Visible in Container
- Confirm GPU is bound to VFIO (Step 5)
- Check IOMMU groups are correct
- Restart the container after modifying `hostpci0`

### llama.cpp OOM Despite GPU
- Check GPU VRAM with `nvidia-smi` inside container
- Reduce `--n-gpu-layers` (e.g., to 40-60 if OOM at 99)
- Ensure `--cpu-moe` is set for CPU offload of MoE experts

### Performance Issues
- Verify GPU is using PCIe 3.0+ (check `lspci -vvv`)
- Check GPU temperature: `nvidia-smi dmon` (should be < 70°C under load)
- If slow, GPU may not be properly initialized; reboot container

## CPU-Fallback Mode

If GPU passthrough is unavailable or causes issues, remove `hostpci0` from the LXC config and set:
```
--cpu-moe --n-gpu-layers 0
```

This uses CPU-offload for MoE experts and CPU inference for the rest. Performance will be slower (~5-10 tok/s vs 30 tok/s), but should work on 8 GB VRAM with 16 GB system RAM.

---

**Reference:** Proxmox Documentation on GPU Passthrough:
https://pve.proxmox.com/wiki/Passthrough_Physical_GPU_to_a_VM
