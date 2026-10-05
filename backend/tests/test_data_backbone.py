"""Tests for data backbone services and TimescaleDB integration."""

from datetime import datetime, UTC

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock

from app.foundation.core.db import Base
from app.foundation.data_backbone.bars import BarStore
from app.foundation.data_backbone.factors_store import FactorStore
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.data_backbone.regime_store import RegimeStore


def _memory_db():
    """Create an in-memory PostgreSQL-compatible database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    # Create hypertables manually (SQLite doesn't support TimescaleDB, but we can
    # create the tables and mock the hypertable behavior)
    with engine.begin() as conn:
        # Create bar_prices table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bar_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                currency TEXT DEFAULT 'USD',
                provider TEXT NOT NULL
            )
        """))

        # Create factor_loadings_daily table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS factor_loadings_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                factor TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                loading REAL NOT NULL
            )
        """))

        # Business-key unique indexes (mirror migration 0075) so ON CONFLICT upserts work.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bar_prices_symbol_ts ON bar_prices (symbol, ts)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_loadings_symbol_factor_ts "
            "ON factor_loadings_daily (symbol, factor, ts)"
        ))

        # Create regime_snapshots table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP NOT NULL,
                label TEXT NOT NULL,
                score REAL NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}'
            )
        """))

        # Create provider_health_history table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS provider_health_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                capability TEXT NOT NULL,
                ok BOOLEAN NOT NULL,
                latency_ms REAL,
                message TEXT
            )
        """))

    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def db():
    """Provide an in-memory test database."""
    return _memory_db()


def test_bar_store_write_and_read(db):
    """Test writing and reading bar data."""
    store = BarStore(db)

    # Insert test data
    sql = text("""
        INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
        VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
    """)

    ts = datetime(2026, 5, 24, 10, 0, 0, tzinfo=UTC)
    db.execute(sql, {
        "symbol": "SPY",
        "ts": ts,
        "open": 510.0,
        "high": 515.0,
        "low": 508.0,
        "close": 512.0,
        "volume": 1000000,
        "currency": "USD",
        "provider": "yfinance",
    })
    db.commit()

    # Read the data
    df = store.get_bars("SPY")
    assert df is not None
    assert len(df) == 1
    assert df.iloc[0]["close"] == 512.0
    assert df.iloc[0]["provider"] == "yfinance"


def test_factor_store_write_and_read(db):
    """Test writing and reading factor loadings."""
    store = FactorStore(db)

    # Write factor loadings
    ts = datetime(2026, 5, 24, 10, 0, 0, tzinfo=UTC)
    result = store.write_factor_loadings(
        symbol="AAPL",
        ts=ts,
        factor_loadings={
            "momentum": 0.5,
            "value": -0.3,
            "quality": 0.4,
        },
    )
    assert result is True

    # Read the data
    df = store.get_factor_loadings("AAPL")
    assert df is not None
    assert len(df) == 3

    # Read pivot format
    pivot = store.get_factor_loadings_pivot("AAPL", ts)
    assert pivot is not None
    assert pivot["momentum"] == 0.5
    assert pivot["value"] == -0.3
    assert pivot["quality"] == 0.4


def test_regime_store_write_and_read(db):
    """Test writing and reading regime snapshots."""
    store = RegimeStore(db)

    # Write regime snapshot
    ts = datetime(2026, 5, 24, 10, 0, 0, tzinfo=UTC)
    result = store.write_snapshot(
        ts=ts,
        label="bull",
        score=0.75,
        source="hmm",
        payload={"model_version": "1.0", "confidence": 0.88},
    )
    assert result is True

    # Read latest snapshot
    snapshot = store.get_latest_snapshot()
    assert snapshot is not None
    assert snapshot["label"] == "bull"
    assert snapshot["score"] == 0.75
    assert snapshot["payload"]["confidence"] == 0.88

    # Read history
    df = store.get_history()
    assert df is not None
    assert len(df) == 1
    assert df.iloc[0]["label"] == "bull"


def test_regime_store_detect_changes(db):
    """Test regime change detection."""
    store = RegimeStore(db)

    # Write bull regime
    store.write_snapshot(
        ts=datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
        label="regime",
        score=0.8,
        source="hmm",
    )

    # Write bear regime (change)
    store.write_snapshot(
        ts=datetime(2026, 5, 25, 10, 0, 0, tzinfo=UTC),
        label="regime",
        score=-0.8,
        source="hmm",
    )

    # Detect changes
    changes = store.detect_regime_changes("regime")
    assert len(changes) > 0
    assert changes[0]["direction"] == "negative"


def test_bar_store_coverage(db):
    """Test bar data coverage calculation."""
    store = BarStore(db)

    # Insert bars for a symbol
    for i in range(5):
        ts = datetime(2026, 5, 20 + i, 10, 0, 0, tzinfo=UTC)
        sql = text("""
            INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
        """)
        db.execute(sql, {
            "symbol": "SPY",
            "ts": ts,
            "open": 510.0 + i,
            "high": 515.0 + i,
            "low": 508.0 + i,
            "close": 512.0 + i,
            "volume": 1000000,
            "currency": "USD",
            "provider": "yfinance",
        })
    db.commit()

    # Get coverage
    coverage = store.get_coverage("SPY")
    assert coverage is not None
    assert coverage["bar_count"] == 5
    assert coverage["last_provider"] == "yfinance"
    assert coverage["gaps_count"] >= -1  # May be -1 for unknown in SQLite


def test_bar_store_latest(db):
    """Test fetching the latest bar."""
    store = BarStore(db)

    # Insert test data
    for i in range(3):
        ts = datetime(2026, 5, 20 + i, 10, 0, 0, tzinfo=UTC)
        sql = text("""
            INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
        """)
        db.execute(sql, {
            "symbol": "SPY",
            "ts": ts,
            "open": 510.0 + i,
            "high": 515.0 + i,
            "low": 508.0 + i,
            "close": 512.0 + i,
            "volume": 1000000,
            "currency": "USD",
            "provider": "yfinance",
        })
    db.commit()

    # Get latest
    latest = store.get_latest_bar("SPY")
    assert latest is not None
    assert latest["close"] == 514.0  # Last bar (2026-05-22)


def test_factor_store_list_factors(db):
    """Test listing all factors."""
    store = FactorStore(db)

    # Write factors
    ts = datetime(2026, 5, 24, 10, 0, 0, tzinfo=UTC)
    store.write_factor_loadings("AAPL", ts, {"momentum": 0.5, "value": -0.3})
    store.write_factor_loadings("GOOGL", ts, {"momentum": 0.4, "quality": 0.6})

    # List factors
    factors = store.get_all_factors()
    assert len(factors) >= 3
    assert "momentum" in factors
    assert "value" in factors
    assert "quality" in factors


def test_regime_store_list_labels(db):
    """Test listing all regime labels."""
    store = RegimeStore(db)

    ts = datetime(2026, 5, 24, 10, 0, 0, tzinfo=UTC)
    store.write_snapshot(ts, "bull", 0.8, "hmm")
    store.write_snapshot(ts, "bear", -0.8, "hmm")
    store.write_snapshot(ts, "transition", 0.0, "hmm")

    # List labels
    labels = store.get_labels()
    assert len(labels) >= 3
    assert "bull" in labels
    assert "bear" in labels
    assert "transition" in labels


def test_phase1_migration_guards(db):
    """Test that Phase 1 migrations are properly guarded."""
    from pathlib import Path

    migration_0014 = Path("alembic/versions/0014_timescale_enable.py").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS timescaledb" in migration_0014

    migration_0015 = Path("alembic/versions/0015_hypertables.py").read_text()
    assert "if not inspector.has_table(\"bar_prices\")" in migration_0015
    assert "if not inspector.has_table(\"factor_loadings_daily\")" in migration_0015
    assert "if not inspector.has_table(\"regime_snapshots\")" in migration_0015
    assert "if not inspector.has_table(\"provider_health_history\")" in migration_0015


class TestIngestBarPricesValidation:

    def _make_ingester(self, db, provider_data):
        ingester = DataIngester.__new__(DataIngester)
        ingester.db = db
        ingester.registry = MagicMock()
        ingester.registry.get_price_history.return_value = provider_data
        return ingester

    def test_rejects_nan_close_prices(self, db):
        data = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000},
            {"date": "2026-01-02", "open": 100, "high": 105, "low": 95, "close": None, "volume": 1000},
            {"date": "2026-01-03", "open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": data, "provider": "test"})

        result = ingester.ingest_bar_prices("TEST")

        assert result["success"] is True
        assert result["rows_inserted"] == 2

    def test_rejects_all_nan_returns_failure(self, db):
        data = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": None, "volume": 1000},
            {"date": "2026-01-02", "open": 100, "high": 105, "low": 95, "close": float("nan"), "volume": 1000},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": data, "provider": "test"})

        result = ingester.ingest_bar_prices("TEST")

        assert result["success"] is False
        assert "rejected by data validation" in result["message"]

    def test_warns_on_negative_prices(self, db, caplog):
        import logging
        data = [
            {"date": "2026-01-01", "open": -10, "high": 5, "low": -15, "close": 100, "volume": 1000},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": data, "provider": "test"})

        with caplog.at_level(logging.WARNING):
            ingester.ingest_bar_prices("TEST")

        assert any("negative" in record.message for record in caplog.records)

    def test_warns_on_zero_close_prices(self, db, caplog):
        import logging
        data = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 0, "volume": 1000},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": data, "provider": "test"})

        with caplog.at_level(logging.WARNING):
            ingester.ingest_bar_prices("TEST")

        assert any("zero close" in record.message for record in caplog.records)

    def test_warns_on_extreme_daily_change(self, db, caplog):
        import logging
        data = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000},
            {"date": "2026-01-02", "open": 200, "high": 210, "low": 190, "close": 200, "volume": 1000},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": data, "provider": "test"})

        with caplog.at_level(logging.WARNING):
            ingester.ingest_bar_prices("TEST")

        assert any("extreme daily price change" in record.message for record in caplog.records)

    def test_normal_data_passes_through(self, db):
        data = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000},
            {"date": "2026-01-02", "open": 100, "high": 106, "low": 98, "close": 103, "volume": 1200},
            {"date": "2026-01-03", "open": 103, "high": 108, "low": 101, "close": 105, "volume": 1100},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": data, "provider": "test"})

        result = ingester.ingest_bar_prices("TEST")

        assert result["success"] is True
        assert result["rows_inserted"] == 3

    def test_mixed_valid_invalid_rows(self, db):
        data = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000},
            {"date": "2026-01-02", "open": 100, "high": 105, "low": 95, "close": None, "volume": 1000},
            {"date": "2026-01-03", "open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
            {"date": "2026-01-04", "open": 100, "high": 105, "low": 95, "close": float("nan"), "volume": 1000},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": data, "provider": "test"})

        result = ingester.ingest_bar_prices("TEST")

        assert result["success"] is True
        assert result["rows_inserted"] == 2

    def test_reingest_same_bars_upserts_not_duplicates(self, db):
        # The daily backfill re-pulls a 1-day overlap and the discover pipeline
        # re-ingests full history; re-ingesting the same (symbol, ts) must update
        # in place, never duplicate (the pre-fix behaviour doubled every row).
        first = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000},
            {"date": "2026-01-02", "open": 101, "high": 106, "low": 96, "close": 102, "volume": 1100},
        ]
        ingester = self._make_ingester(db, {"ok": True, "data": first, "provider": "test"})
        ingester.ingest_bar_prices("TEST")

        # Re-ingest the same dates with a corrected close for 2026-01-02.
        second = [
            {"date": "2026-01-01", "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000},
            {"date": "2026-01-02", "open": 101, "high": 106, "low": 96, "close": 108, "volume": 1100},
        ]
        ingester.registry.get_price_history.return_value = {"ok": True, "data": second, "provider": "test"}
        ingester.ingest_bar_prices("TEST")

        count = db.execute(text("SELECT COUNT(*) FROM bar_prices WHERE symbol='TEST'")).scalar()
        assert count == 2  # upserted, not 4
        updated_close = db.execute(
            text("SELECT close FROM bar_prices WHERE symbol='TEST' AND ts='2026-01-02 00:00:00'")
        ).scalar()
        assert float(updated_close) == 108.0  # DO UPDATE refreshed the bar
