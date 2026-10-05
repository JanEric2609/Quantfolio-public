"""Tests for the AlphaCrafter cross-sectional panel builder + compute_factor wiring."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering.fundamentals_loader import load_fundamentals_extract
from app.foundation.data_engineering.security_identifiers_loader import (
    load_security_identifiers_extract,
)
from app.foundation.models.entities import Security, SecurityListing
from app.lab.alphacrafter.panel import (
    FUNDAMENTAL_FIELDS,
    PRICE_FIELDS,
    _extract_fundamentals,
    build_panel,
)
from app.foundation.quant_factors import compute_factor


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # bar_prices is a raw-SQL hypertable accessed via BarStore; create it explicitly.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS bar_prices (
                    symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,
                    close REAL, volume REAL, currency TEXT, provider TEXT
                )
                """
            )
        )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_bars(db, symbols, days=30):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    rng = np.random.default_rng(7)
    for s_i, sym in enumerate(symbols):
        price = 100.0 + s_i * 10
        for d in range(days):
            price *= 1 + rng.normal(0.001, 0.01)
            ts = base + timedelta(days=d)
            db.execute(
                text(
                    """
                    INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
                    VALUES (:symbol, :ts, :o, :h, :l, :c, :v, 'USD', 'test')
                    """
                ),
                {
                    "symbol": sym,
                    "ts": ts,
                    "o": price,
                    "h": price * 1.01,
                    "l": price * 0.99,
                    "c": price,
                    "v": 1_000_000 + d,
                },
            )
    db.commit()


class _FakeRegistry:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_fundamentals(self, symbol):
        return {"ok": True, "data": self._mapping.get(symbol, {})}


def test_extract_fundamentals_flat_and_nested():
    flat = _extract_fundamentals({"pe_ratio": 12.0, "market_cap": 5e9, "roe": 0.2})
    assert flat == {"pe_ratio": 12.0, "market_cap": 5e9, "roe": 0.2}

    nested = _extract_fundamentals(
        {"Highlights": {"PERatio": "15", "MarketCapitalization": "1000", "ReturnOnEquityTTM": "0.3"}}
    )
    assert nested["pe_ratio"] == 15.0
    assert nested["market_cap"] == 1000.0
    assert nested["roe"] == 0.3

    missing = _extract_fundamentals({})
    assert missing == {"pe_ratio": None, "market_cap": None, "roe": None}


def test_build_panel_shapes_and_fundamentals():
    db = _memory_db()
    symbols = ["AAA", "BBB", "CCC"]
    _seed_bars(db, symbols, days=30)
    registry = _FakeRegistry(
        {
            "AAA": {"pe_ratio": 10.0, "market_cap": 1e9, "roe": 0.15},
            "BBB": {"pe_ratio": 20.0, "market_cap": 2e9, "roe": 0.25},
            # CCC intentionally missing -> NaN fundamentals
        }
    )
    panel = build_panel(
        db,
        symbols,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
        registry=registry,
    )

    for field in PRICE_FIELDS + FUNDAMENTAL_FIELDS:
        assert field in panel, f"missing {field}"
        assert list(panel[field].columns) == symbols

    assert panel["close"].shape[0] == 30
    # Fundamentals broadcast across the date index.
    assert (panel["pe_ratio"]["AAA"] == 10.0).all()
    assert (panel["pe_ratio"]["BBB"] == 20.0).all()
    assert panel["pe_ratio"]["CCC"].isna().all()


def test_build_panel_prefers_point_in_time_fundamentals_when_resolvable(tmp_path, monkeypatch):
    """A symbol resolvable to a gvkey via security_master gets real PIT history;
    an unresolvable one keeps the old registry-broadcast behaviour."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    identifiers_csv = tmp_path / "identifiers.csv"
    identifiers_csv.write_text("gvkey,isin,fic,datadate\n1001,DE0001,DEU,2024-01-01\n")
    assert load_security_identifiers_extract(identifiers_csv, dry_run=False).ok is True

    fundamentals_csv = tmp_path / "fundamentals.csv"
    fundamentals_csv.write_text(
        "gvkey,datadate,fyear,indfmt,consol,popsrc,datafmt,curcd,at,ceq,ni,revt,csho,epspx,dltt\n"
        "1001,2023-06-30,2023,INDL,C,I,STD,EUR,500.0,100.0,20.0,300.0,50.0,2.0,80.0\n"
    )
    assert load_fundamentals_extract(fundamentals_csv, dry_run=False).ok is True

    db = _memory_db()
    security = Security(isin="DE0001", canonical_name="AAA Inc", base_currency="EUR")
    db.add(security)
    db.flush()
    db.add(
        SecurityListing(
            security_id=security.id, mic="XETR", exchange_label="XETRA", symbol="AAA", currency="EUR"
        )
    )
    db.commit()

    symbols = ["AAA", "BBB"]
    _seed_bars(db, symbols, days=30)
    registry = _FakeRegistry({"BBB": {"pe_ratio": 20.0, "market_cap": 2e9, "roe": 0.25}})

    panel = build_panel(
        db,
        symbols,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
        registry=registry,
    )

    # AAA: PIT-covered -- available_from = 2023-06-30 + 6mo = 2023-12-30, so
    # every date in the (2024-01-01..) window sees the fundamentals row, and
    # the values move with price rather than sitting at one broadcast scalar.
    close_aaa = panel["close"]["AAA"]
    expected_pe = close_aaa / 2.0
    assert np.allclose(panel["pe_ratio"]["AAA"].to_numpy(), expected_pe.to_numpy())
    assert np.allclose(panel["roe"]["AAA"].to_numpy(), 20.0 / 100.0)
    assert not (panel["pe_ratio"]["AAA"] == panel["pe_ratio"]["AAA"].iloc[0]).all(), (
        "PIT pe_ratio should vary with price, not be a single broadcast scalar"
    )

    # BBB: no SecurityListing -> falls back to the registry broadcast, unchanged.
    assert (panel["pe_ratio"]["BBB"] == 20.0).all()


def test_build_panel_empty_universe_returns_empty():
    db = _memory_db()
    panel = build_panel(
        db,
        ["NOPE"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
        include_fundamentals=False,
    )
    assert panel == {}


def test_compute_factor_nan_without_fundamentals():
    """Fundamental factors emit all-NaN (not raise) when the column is missing."""
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    prices = pd.DataFrame({"close": np.linspace(100, 110, 10)}, index=idx)
    for name in ("value_ep", "size_log_mc", "quality_roe"):
        series = compute_factor(name, prices)
        assert series.isna().all(), f"{name} should be all-NaN, got {series.dropna().tolist()}"


def test_compute_factor_uses_fundamentals_when_present():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    prices = pd.DataFrame(
        {
            "close": np.linspace(100, 110, 5),
            "pe_ratio": np.full(5, 20.0),
            "market_cap": np.full(5, np.e),  # log -> 1.0
            "roe": np.full(5, 0.18),
        },
        index=idx,
    )
    assert np.allclose(compute_factor("value_ep", prices).to_numpy(), 0.05)
    assert np.allclose(compute_factor("size_log_mc", prices).to_numpy(), 1.0)
    assert np.allclose(compute_factor("quality_roe", prices).to_numpy(), 0.18)
