---
name: pr-check
description: Run pre-PR validation — lint, type-check, tests, migration dry-run, and security review, then print go/no-go verdict
disable-model-invocation: true
---

Run all pre-PR validation checks for Quantfolio. Commands run from project root.

## Checks (run in parallel where possible)

### 1. Lint (Ruff)

```bash
cd backend && .venv/bin/ruff check app/ && echo "PASS" || echo "FAIL"
```

### 2. TypeScript type-check

```bash
cd frontend && npx tsc --noEmit && echo "PASS" || echo "FAIL"
```

### 3. Backend tests

```bash
cd backend && .venv/bin/pytest -x -q 2>&1 | tail -5
```

Exit code: 0 = PASS, non-zero = FAIL.

### 4. Frontend tests

```bash
cd frontend && npx vitest run 2>&1 | tail -5
```

Exit code: 0 = PASS, non-zero = FAIL.

### 5. Migration dry-run (if migrations changed)

If PR touches `backend/alembic/versions/`:

```bash
cd backend && TMPDB=$(mktemp /tmp/quantfolio_pr_XXXXXX.db) && DATABASE_URL="sqlite:///$TMPDB" .venv/bin/alembic upgrade head 2>&1 | tail -10; rm -f "$TMPDB"
```

### 6. Security review (if auth/DKB/secrets touched)

If PR touches `app/interface/api/auth.py`, `app/foundation/dkb/`, `app/foundation/settings.py`, or `app/foundation/core/security.py`, invoke `@security-reviewer` with the diff.

### 7. Migration validator (if migrations changed)

If PR touches `backend/alembic/versions/`, invoke `@migration-validator` with the changed files.

## Verdict

```
═══════════════════════════════════════
  QUANTFOLIO PR CHECK
═══════════════════════════════════════

  Ruff lint:          PASS
  TypeScript:         PASS
  Backend tests:      PASS (42 passed)
  Frontend tests:     PASS (18 passed)
  Migration dry-run:  PASS
  Security review:    PASS
  Migration validate: PASS

  VERDICT: ✅ PR READY FOR MERGE
═══════════════════════════════════════
```

Any FAIL blocks the verdict — list each failure with file/line references and one-line guidance to fix.
