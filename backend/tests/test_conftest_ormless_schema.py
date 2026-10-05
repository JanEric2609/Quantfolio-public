"""The shared test fixture must build the same schema production runs.

Four TimescaleDB hypertables (``bar_prices``, ``factor_loadings_daily``,
``regime_snapshots``, ``provider_health_history``) have no ORM model, so
``Base.metadata.create_all()`` does not create them and ``tests/conftest.py``
writes their DDL by hand.

That hand-written DDL had drifted badly: ``factor_loadings_daily`` was created
wide (one column per factor) where the real schema is long
(``symbol, factor, ts, loading``), and ``provider_health_history`` was created
with ``status``/``error_message`` where the real schema has
``capability``/``ok``/``message``. Both write paths swallow their exceptions, so
against that fixture they were silent no-ops — the suite stayed green while the
code under test never wrote a row.

These tests exercise the real write paths and assert a row actually lands, so
the fixture cannot drift away from production again without failing loudly.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from conftest import _memory_db

# The exact column sets created by alembic/versions/0015_hypertables.py.
EXPECTED_COLUMNS = {
    "bar_prices": {
        "id", "symbol", "ts", "open", "high", "low", "close",
        "volume", "currency", "provider",
    },
    "factor_loadings_daily": {"id", "symbol", "factor", "ts", "loading"},
    "regime_snapshots": {"id", "ts", "label", "score", "source", "payload_json"},
    "provider_health_history": {
        "id", "provider", "ts", "capability", "ok", "latency_ms", "message",
    },
}


def _columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(text(f"PRAGMA table_info({table})"))}


def test_fixture_columns_match_the_migrations():
    db = _memory_db()
    for table, expected in EXPECTED_COLUMNS.items():
        assert _columns(db, table) == expected, f"{table} drifted from the migration DDL"


def test_provider_health_write_path_actually_writes():
    """The regression: this INSERT failed with `no column named capability`."""
    from app.foundation.data_backbone.ingest import DataIngester

    db = _memory_db()
    DataIngester(db)._record_provider_health(
        provider="yfinance",
        capability="bar_prices",
        ok=True,
        message="ok",
        latency_ms=12.5,
    )

    rows = db.execute(
        text("SELECT provider, capability, ok, message FROM provider_health_history")
    ).fetchall()
    assert len(rows) == 1, "health event was silently swallowed"
    assert rows[0][0] == "yfinance"
    assert rows[0][1] == "bar_prices"


def test_factor_store_write_path_actually_writes():
    """The regression: this INSERT failed with `no column named factor`."""
    from app.foundation.data_backbone.factors_store import FactorStore

    db = _memory_db()
    ts = datetime(2026, 5, 24, tzinfo=UTC)

    assert FactorStore(db).write_factor_loadings(
        symbol="AAPL", ts=ts, factor_loadings={"momentum": 0.5, "value": -0.3}
    ) is True

    rows = db.execute(
        text("SELECT symbol, factor, loading FROM factor_loadings_daily ORDER BY factor")
    ).fetchall()
    assert len(rows) == 2, "factor loadings were silently swallowed"
    assert rows[0] == ("AAPL", "momentum", 0.5)
    assert rows[1] == ("AAPL", "value", -0.3)


def test_factor_store_upsert_needs_the_unique_index():
    """`ON CONFLICT (symbol, factor, ts)` requires the business-key unique index."""
    from app.foundation.data_backbone.factors_store import FactorStore

    db = _memory_db()
    ts = datetime(2026, 5, 24, tzinfo=UTC)
    store = FactorStore(db)

    assert store.write_factor_loadings(symbol="AAPL", ts=ts, factor_loadings={"momentum": 0.5})
    assert store.write_factor_loadings(symbol="AAPL", ts=ts, factor_loadings={"momentum": 0.9})

    rows = db.execute(text("SELECT loading FROM factor_loadings_daily")).fetchall()
    assert len(rows) == 1, "upsert duplicated instead of updating"
    assert rows[0][0] == 0.9


def test_ormless_tables_reject_nulls_the_way_production_does():
    """NOT NULL is part of the contract — a fixture that relaxes it hides bugs."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    db = _memory_db()
    with pytest.raises(IntegrityError):
        db.execute(
            text("INSERT INTO provider_health_history (provider, ts) VALUES ('x', '2026-01-01')")
        )
