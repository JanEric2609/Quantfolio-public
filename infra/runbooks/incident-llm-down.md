# Incident Runbook: Local LLM Down

**Severity:** Medium (services degrade gracefully; cloud fallback available)  
**Detection:** Healthcheck shows `llm_local: down`  
**Typical MTTR:** 5-10 minutes  

## Detection

```bash
# Run healthcheck
./infra/scripts/healthcheck.sh verbose

# Look for:
# "llm_local": "down"
```

Or from logs:
```bash
pct exec 111 journalctl -u quantfolio-api -n 20 | grep -i "llm\|error"
```

## Immediate Triage (1 min)

### 1. Check Container Status

```bash
# On Proxmox host
pct status 112
# Should show: lxc/112 (quantfolio-llm) is running
```

### 2. SSH into LLM Container

```bash
pct exec 112 bash
systemctl status quantfolio-llamacpp
```

Expected output: `active (running)` or `inactive (dead)`

### 3. Check System Resources

```bash
# Inside LLM container
free -h            # RAM usage
df -h              # Disk space
nvidia-smi         # GPU status (if available)
```

## Diagnosis

### Scenario A: Service Crashed (inactive)

```bash
systemctl start quantfolio-llamacpp
journalctl -u quantfolio-llamacpp -n 50 | tail -20
```

**Common causes:**
- OOM (Out of Memory): Check `free -h` output
- GPU OOM: Check `nvidia-smi` output
- Missing model file: Check `/var/lib/quantfolio/models/`

### Scenario B: Service Running but Not Responding

```bash
# Still inside LLM container
curl -v http://<LLM_HOST>:8080/health   # your LLAMA_BIND_HOST (/health needs no API key)
# Should return 200 OK

# If connection refused:
ss -tlnp | grep 8080
# Should show: llama-server listening on your LLAMA_BIND_HOST:8080 (0.0.0.0 if unset)
```

**Common causes:**
- Port binding failed: Another process on 8080
- Hung process: Inference stuck in loop
- Memory leak: llama-server using > 16 GB

### Scenario C: Model Loading Timeout

```bash
# Check systemd timeout
systemctl show quantfolio-llamacpp | grep TimeoutStartSec
# Edit /etc/systemd/system/quantfolio-llamacpp.service if needed

# Check journal for loading progress
journalctl -u quantfolio-llamacpp -f
```

## Recovery Procedures

### Fix A: OOM Issue

**If RAM usage shows > 15 GB and llama.cpp crashed:**

```bash
# Inside LLM container

# 1. Free RAM
sync && echo 3 > /proc/sys/vm/drop_caches

# 2. Reduce quantization (if swap is full)
# Download lighter model: Q3_K_M instead of Q4_K_M
# Then update /etc/systemd/system/quantfolio-llamacpp.service

# 3. Reduce context size
# Edit /etc/systemd/system/quantfolio-llamacpp.service:
# --ctx-size 4096  (instead of 8192)

# 4. Reload and restart
systemctl daemon-reload
systemctl restart quantfolio-llamacpp
```

### Fix B: Port Binding Conflict

```bash
# Find process using port 8080
ss -tlnp | grep 8080
# Kill if necessary: kill -9 <pid>

# Restart service
systemctl restart quantfolio-llamacpp
```

### Fix C: GPU Memory Exhaustion

```bash
# Inside LLM container
nvidia-smi

# If GPU memory full:
# 1. Reduce --n-gpu-layers (from 99 to 40-60)
# 2. Edit /etc/systemd/system/quantfolio-llamacpp.service
# 3. Reload and restart

systemctl daemon-reload
systemctl restart quantfolio-llamacpp

# Monitor recovery
watch -n 1 nvidia-smi
```

## Fallback Behavior (User Impact)

While local LLM is down, the app automatically:

1. **Batch research tasks** (AlphaCrafter Miner, dossier synthesis, Financial CoT):
   - Route to Anthropic Claude (if API key configured)
   - Or OpenAI (if configured)
   - Or mark as `degraded_quality = True` and wait for retry

2. **Interactive tasks** (chat, budget agent):
   - Return error: "Local LLM unavailable and no cloud fallback"
   - User is notified via bell (if configured)

3. **Routine tasks** (sentiment, summarization):
   - Queued with lower priority
   - Wait until local LLM is back up

## Verification (5 min)

```bash
# 1. Check service is running
pct exec 112 systemctl status quantfolio-llamacpp

# 2. Test health endpoint
pct exec 112 curl http://<LLM_HOST>:8080/health

# 3. Run inference test
pct exec 112 curl -X POST http://<LLM_HOST>:8080/v1/completions \
  -H "Authorization: Bearer $LLAMA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "quantfolio", "prompt": "test", "max_tokens": 5}'

# 4. Run full healthcheck
./infra/scripts/healthcheck.sh verbose
# Should show: "llm_local": "ok"
```

## Notification to Users

Update Settings → AI Research:
- "Local LLM status: Recovering (ETA 5 min)"
- "Using cloud fallback (Anthropic) for analysis tasks"

## Post-Incident

1. **Check logs for root cause:**
   ```bash
   journalctl -u quantfolio-llamacpp --since "1 hour ago" > /tmp/llm-logs.txt
   ```

2. **Document:** Add to `INCIDENTS.md` with:
   - Start time, duration, cause
   - Resolution steps taken
   - Preventive action (if any)

3. **Monitor:** Watch healthcheck output for 1 hour to ensure stability

## Escalation

If unable to recover after 30 min:

1. **Restart container:**
   ```bash
   pct stop 112
   sleep 5
   pct start 112
   ```

2. **Check GPU health** (if using GPU):
   ```bash
   pct exec 112 nvidia-smi -pm 1  # Enable persistence mode
   pct exec 112 nvidia-smi -pC 0,0  # Reset clocks if needed
   ```

3. **Rollback to CPU-only:**
   - Remove `--n-gpu-layers 99` from service
   - Set `--n-gpu-layers 0 --cpu-moe`
   - Throughput will drop to ~5 tok/sec, but system will run

## Related

- `deploy.md` — LLM container deployment
- `moe-offload-notes.md` — Memory/GPU tuning
- `gpu-passthrough.md` — GPU troubleshooting

---

**Last updated:** 2026-05-24
