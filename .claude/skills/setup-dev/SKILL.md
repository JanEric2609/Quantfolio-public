---
name: setup-dev
description: Check prerequisites and set up the Quantfolio development environment — Python venv, frontend npm, database init, and migrations
disable-model-invocation: true
---

Set up the Quantfolio development environment from scratch.

## Step 1 — Check prerequisites

```bash
echo "=== Python ===" && python3 --version 2>/dev/null || echo "MISSING"
echo "=== Node ===" && node --version 2>/dev/null || echo "MISSING"
echo "=== npm ===" && npm --version 2>/dev/null || echo "MISSING"
echo "=== PostgreSQL (optional) ===" && psql --version 2>/dev/null || echo "not found — will use SQLite"
```

Requirements: Python ≥3.11, Node ≥18, npm ≥9.

## Step 2 — Backend setup

```bash
cd backend

# Create virtual environment if missing
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

# Install dependencies (dev includes pytest, ruff)
.venv/bin/pip install -e ".[dev]"

# Check for optional extras
echo "=== OpenBB ===" && .venv/bin/python -c "import openbb" 2>/dev/null && echo "OK" || echo "not installed (optional — skip with --no-openbb)"
echo "=== ML extras ===" && .venv/bin/python -c "import torch" 2>/dev/null && echo "OK" || echo "not installed (optional)"
```

## Step 3 — Database setup

```bash
cd backend

# Try PostgreSQL first, fall back to SQLite
if command -v psql &>/dev/null; then
    echo "PostgreSQL found. Creating database if needed..."
    createdb quantfolio 2>/dev/null && echo "createdb OK" || echo "database may already exist"
    DATABASE_URL="postgresql://localhost/quantfolio"
else
    echo "No PostgreSQL — using SQLite"
    DATABASE_URL="sqlite:///./quantfolio.db"
fi

# Run migrations
DATABASE_URL="$DATABASE_URL" .venv/bin/alembic upgrade head && echo "Migrations OK" || echo "Migration failed — check DATABASE_URL"
```

## Step 4 — Frontend setup

```bash
cd frontend
if [ ! -d node_modules ]; then
    npm install
fi
```

## Step 5 — Verify

```bash
# Backend starts
cd backend && timeout 5 .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
sleep 2
curl -sf http://127.0.0.1:8000/api/health && echo " — Backend OK" || echo "Backend FAIL"
kill $BACKEND_PID 2>/dev/null

# Frontend builds
cd frontend && npx tsc --noEmit && echo "TypeScript OK" || echo "TypeScript FAIL"
```

## Output format

```
✓ Python 3.11.6
✓ Node 20.11.0
✓ pip install -e ".[dev]" done
✓ PostgreSQL database ready
✓ alembic upgrade head done
✓ npm install done
✓ Backend health check passed
✓ TypeScript check passed
→ Run: cd backend && .venv/bin/uvicorn app.main:app --reload  (port 8000)
→ Run: cd frontend && npm run dev  (port 5173)
```
