# Incident Runbook: Cloud API Keys Rotated

**Severity:** Medium (cloud fallback stops working; local LLM still functional)  
**Detection:** Batch research tasks fail with auth error  
**Typical MTTR:** 5 minutes  

## Detection

### Error Symptoms

1. **In app logs:**
   ```
   ERROR: Anthropic API key invalid (401)
   ERROR: batch_research task routed to cloud but key unauthorized
   ```

2. **From healthcheck:**
   ```bash
   ./infra/scripts/healthcheck.sh verbose
   # Output: "llm_anthropic": "down" or "llm_openai": "down"
   ```

3. **In audit_logs table:**
   ```sql
   SELECT * FROM audit_logs WHERE event_type = 'llm_call_failed' ORDER BY ts DESC LIMIT 5;
   ```

4. **User-facing:** Batch research tasks hang or error out

## Immediate Triage (1 min)

### 1. Check Which Key Failed

```bash
# From app container (111), check env:
echo $ANTHROPIC_API_KEY
echo $OPENAI_API_KEY

# Or check database (keys are stored encrypted)
psql -h <DB_HOST> -U quantfolio_user -d quantfolio -c "SELECT service FROM api_keys;"
```

### 2. Verify Cloud Endpoints Are Reachable

```bash
# Test Anthropic
curl -s https://api.anthropic.com/v1/health \
  -H "Authorization: Bearer $ANTHROPIC_API_KEY" | jq '.'

# Test OpenAI  
curl -s https://api.openai.com/v1/health \
  -H "Authorization: Bearer $OPENAI_API_KEY" | jq '.'

# Both should return 200 or 401 (401 is expected if key invalid)
```

### 3. Check Auth Error Specifically

```bash
# Test Anthropic with known bad key
curl -i -X POST https://api.anthropic.com/v1/messages \
  -H "Authorization: Bearer sk-invalid-test" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus", "messages": [{"role": "user", "content": "test"}], "max_tokens": 10}'

# Should return: 401 Unauthorized
```

## Recovery: Update Keys in Settings

### Option A: Via Web UI (Recommended)

1. **Open Settings → Integrations**
2. **Click "Edit" on Anthropic or OpenAI**
3. **Paste new API key**
4. **Click "Save"**
5. **System stores encrypted in `api_keys` table**

### Option B: Via Database (Direct)

If web UI is unavailable:

```bash
# Inside app container (111)
cd /opt/quantfolio/backend

# Create temporary Python script
cat > /tmp/rotate_key.py << 'EOF'
import sys
sys.path.insert(0, '/opt/quantfolio/backend')

from app.core.db import SessionLocal
from app.services.settings import set_secret

db = SessionLocal()

# Replace with your new key
NEW_ANTHROPIC_KEY = "sk-ant-v7-xxxxxx..."

# Update in database (encrypted)
set_secret(db, "anthropic", NEW_ANTHROPIC_KEY, meta={"rotated_at": "2026-05-24"})

print("✓ Anthropic key rotated in database")
db.close()
EOF

# Run script
source .venv/bin/activate
python /tmp/rotate_key.py
```

### Option C: Via Environment Variable (Temporary)

For immediate testing:

```bash
# Edit /opt/quantfolio/.env
ANTHROPIC_API_KEY=sk-ant-v7-xxxxx...

# Restart app
systemctl restart quantfolio-api
```

Note: This writes to disk. For production, use Option A or B.

## Verification (2 min)

### 1. Test Cloud Connection

```bash
# From app container
curl -X POST http://localhost:8000/api/finagent/llm/health \
  -H "Content-Type: application/json"

# Should return all backends with health status
{
  "llm_anthropic": "ok",
  "llm_openai": "ok",
  "llm_local": "ok"
}
```

### 2. Test Batch Research Task

Trigger a small batch task:

```bash
# From any container with curl
curl -X POST http://<APP_HOST>:8000/api/alphacrafter/miner/run \
  -H "Content-Type: application/json" \
  -d '{"universe": ["SPY", "QQQ"], "batch_mode": true}'

# Monitor audit_logs for completion
psql -h <DB_HOST> -U quantfolio_user -d quantfolio \
  -c "SELECT event_type, severity, payload_json FROM audit_logs WHERE event_type LIKE '%miner%' ORDER BY ts DESC LIMIT 1;"
```

### 3. Run Full Healthcheck

```bash
./infra/scripts/healthcheck.sh verbose

# Should show: "llm_anthropic": "ok"
```

## Preventive Measures

### 1. Key Rotation Schedule

Set calendar reminders to rotate keys monthly:
- Anthropic: Every 1st of month
- OpenAI: Every 15th of month

### 2. Audit Logging

Verify audit logs capture key rotation:

```sql
SELECT event_type, ts, payload_json FROM audit_logs 
WHERE event_type = 'api_key_updated' 
ORDER BY ts DESC LIMIT 5;
```

### 3. Automated Expiration Detection

In future version, add job to detect and notify on:
- API key expiration (if provider supports)
- Successful vs failed auth rates

## Impact During Outage

While cloud keys are invalid:

| Task Type | Behavior |
|-----------|----------|
| **batch_research** | Falls back to local Qwen3.6-35B-A3B (slower, lower quality) |
| **interactive** | Stays on local LLM only (as designed) |
| **routine** | Stays on local LLM only |

**Net impact:** ~5-10% quality degradation for research tasks; no user-facing failures.

## Post-Incident

1. **Document in INCIDENTS.md:**
   - When keys were rotated
   - Which provider
   - Reason (scheduled rotation / security incident)

2. **Review rotation process:**
   - Was Settings UI accessible?
   - Did healthcheck catch the issue immediately?
   - Any delayed user notifications?

3. **Update runbooks** if new patterns observed

## Related

- `deploy.md` — Initial key setup (Section 3, Stage 2)
- `backup-restore.md` — Backup includes encrypted keys

---

**Last updated:** 2026-05-24
