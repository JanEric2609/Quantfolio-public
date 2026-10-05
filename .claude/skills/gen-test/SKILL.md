---
name: gen-test
description: Generate a pytest test file for a Quantfolio backend service or API module following project conventions — _memory_db helper, no DB mocks, real in-process SQLite
---

Generate tests for: {{target}}

## Boilerplate every test file starts with

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()
```

Import only what you need from the module under test plus any model entities. Check `app/foundation/models/entities/` (re-exported from its `__init__.py`) for the exact model names.

## Rules

1. **Never mock the database** — use `_memory_db()` so tests exercise real SQL. The in-memory SQLite DB is fast enough.
2. **One `_memory_db()` call per test** — don't share sessions across tests; create a fresh one in each test function or in a local fixture.
3. **Test the happy path and at least one failure mode** per function.
4. **For services with encrypted secrets**, create a `Settings` fixture:
   ```python
   from app.foundation.core.config import Settings
   settings = Settings(jwt_secret="test-secret", encryption_key=None)
   ```
5. **For API-layer tests** use `httpx.AsyncClient` with `app` from `app.main` — not direct service calls.
6. **Quant/ML tests**: mock external price fetches (yfinance, finnhub) but keep all pandas/numpy computation real.
7. **Tax tests**: always assert that responses include `estimate=True` and `not_tax_advice=True`.
8. **File name**: `tests/test_<module_name>.py`. Run with `.venv/bin/pytest tests/test_<module_name>.py -v`.

## Example: service test

```python
from app.foundation.settings import get_secret, set_secret
from app.foundation.models.entities import User


def test_secret_survives_round_trip():
    db = _memory_db()
    set_secret(db, "dkb", "s3cr3t", {"username": "alice"})

    value, meta = get_secret(db, "dkb")

    assert value == "s3cr3t"
    assert meta["username"] == "alice"


def test_missing_secret_returns_none():
    db = _memory_db()

    value, meta = get_secret(db, "nonexistent")

    assert value is None
```

## Example: settings/catalog test

```python
from app.foundation.settings import upsert_public_settings, get_public_settings


def test_public_settings_persist():
    db = _memory_db()
    upsert_public_settings(db, {"llm_model": "gpt-4o"})

    settings = get_public_settings(db)

    assert settings["llm_model"] == "gpt-4o"
```
