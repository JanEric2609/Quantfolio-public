---
name: performance-audit
description: Profile endpoint latency, MLflow experiments, and resource usage for Quantfolio's quant/ML pipeline
disable-model-invocation: true
---

Profile performance across Quantfolio's quant and ML pipeline. All commands run from `backend/`.

## Checks

### 1. API endpoint latency (quick scan)

```bash
cd backend && .venv/bin/python -c "
import httpx, time, sys
endpoints = [
    '/api/health',
    '/api/quant/frontier',
    '/api/quant/metrics',
    '/api/quant/monte-carlo',
    '/api/quant/factors',
    '/api/tax/cockpit',
    '/api/portfolio/holdings',
]
base = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8000'
for ep in endpoints:
    t0 = time.time()
    try:
        r = httpx.get(f'{base}{ep}', timeout=10)
        ms = (time.time() - t0) * 1000
        status = 'SLOW' if ms > 2000 else ('OK' if ms < 500 else 'WARN')
        print(f'{status:5s} {ms:8.0f}ms {ep}')
    except Exception as e:
        print(f'FAIL  {ep} — {e}')
"
```

### 2. MLflow experiment count & drift

```bash
cd backend && .venv/bin/python -c "
from mlflow import MlflowClient
try:
    c = MlflowClient()
    exps = c.search_experiments()
    print(f'Experiments: {len(exps)}')
    for exp in sorted(exps, key=lambda e: e.last_update_time, reverse=True)[:5]:
        runs = c.search_runs(exp.experiment_id, max_results=1)
        if runs:
            print(f'  {exp.name}: last run {runs[0].end_time}')
except Exception as e:
    print(f'MLflow unavailable — {e}')
"
```

### 3. Model training resource baseline

```bash
cd backend && .venv/bin/time --format='real %e user %U sys %S mem %MKB' \
  .venv/bin/python -c "
from app.foundation.quant_metrics import compute_sortino, compute_cvar
from app.foundation.quant import monte_carlo_simulation
import numpy as np
rets = np.random.randn(1000, 10) * 0.01
compute_sortino(rets, 0.05)
compute_cvar(rets, 0.05)
monte_carlo_simulation(rets, 10000)
print('Quant baseline OK')
"
```

### 4. Memory footprint of active services

```bash
ps aux | grep -E '(uvicorn|quantfolio|caddy|postgres|redis)' | awk '{printf "%-30s %5s MB\n", $11, $6/1024}'
```

### 5. SQL query slowness (requires pg_stat_statements on Postgres)

```bash
cd backend && .venv/bin/python -c "
from sqlalchemy import text
from app.foundation.core.db import SessionLocal
with SessionLocal() as s:
    rows = s.execute(text(\"\"\"
        SELECT query, mean_exec_time, calls
        FROM pg_stat_statements
        ORDER BY mean_exec_time DESC
        LIMIT 10
    \"\")).fetchall()
    for row in rows[:5]:
        print(f'{row.mean_exec_time:8.1f}ms x{row.calls} — {row.query[:80]}')
" 2>/dev/null || echo 'pg_stat_statements not available — skip SQL profiling'
```

## Interpretation

| Metric | Threshold | Action |
|--------|-----------|--------|
| Endpoint latency | >2000ms | Profile with cProfile / py-spy |
| MLflow experiments | >50 | Archive stale experiments |
| Training memory | >2GB | Reduce batch size / feature dim |
| SQL mean time | >500ms | Add index / optimize query |

## Output format

```
OK    124ms  /api/health
WARN  1420ms /api/quant/frontier
SLOW  3400ms /api/quant/monte-carlo
...
```

End with a summary of any SLOW or WARN items and recommended actions.
