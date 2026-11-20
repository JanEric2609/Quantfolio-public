"""Tests for exporting bar_prices into the PIT Parquet panel (panel_export.py)."""
from datetime import datetime, UTC

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering.panel_export import export_panel


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
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
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_bars(db, symbol: str, n: int = 3):
    sql = text("""
        INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
        VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
    """)
    for i in range(n):
        db.execute(sql, {
            "symbol": symbol,
            "ts": datetime(2026, 1, i + 1, tzinfo=UTC),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000 + i,
            "currency": "EUR",
            "provider": "yfinance",
        })
    db.commit()


def test_export_panel_dry_run_reports_counts_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()
    _seed_bars(db, "IWDA.AS", n=3)

    counts = export_panel(db, dry_run=True)

    assert counts == {"IWDA.AS": 3}
    assert not (tmp_path / "internal").exists()


def test_export_panel_apply_writes_parquet_partition_per_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()
    _seed_bars(db, "IWDA.AS", n=2)
    _seed_bars(db, "VWCE.DE", n=1)

    counts = export_panel(db, dry_run=False)

    assert counts == {"IWDA.AS": 2, "VWCE.DE": 1}
    written = sorted(p.name for p in (tmp_path / "internal").glob("*.parquet"))
    assert written == ["IWDA.AS.parquet", "VWCE.DE.parquet"]

    frame = pd.read_parquet(tmp_path / "internal" / "IWDA.AS.parquet")
    assert len(frame) == 2
    assert (frame["source"] == "internal").all()
    assert frame["symbol"].unique().tolist() == ["IWDA.AS"]


def test_export_panel_skips_symbols_with_no_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()

    counts = export_panel(db, symbols=["NOTHING.HERE"], dry_run=True)

    assert counts == {"NOTHING.HERE": 0}
