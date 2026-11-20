"""Dry-run scan of the 2026-10 bar repair script (plan WP1d)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import text

from conftest import _memory_db

_spec = importlib.util.spec_from_file_location(
    "repair_bars_2026_10", Path(__file__).resolve().parent.parent / "scripts" / "repair_bars_2026_10.py",
)
repair = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(repair)  # type: ignore[union-attr]


def _bar(db, symbol, day, close, provider):
    db.execute(text(
        "INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider) "
        "VALUES (:s, :ts, :c, :c, :c, :c, 0, 'USD', :p)"
    ), {"s": symbol, "ts": f"{day} 00:00:00", "c": close, "p": provider})


def test_scan_finds_splice_and_christmas_bar():
    db = _memory_db()
    _bar(db, "SPY", "2021-10-12", 434.7, "twelvedata")
    _bar(db, "SPY", "2021-10-13", 466.0, "yfinance")
    _bar(db, "SPY", "2025-12-24", 600.0, "yfinance")
    _bar(db, "SPY", "2025-12-25", 600.5, "unknown")
    db.commit()

    plan = repair.scan_symbol(db, "SPY")

    assert plan["repull"] is True
    assert plan["providers"] == ["twelvedata", "unknown", "yfinance"]
    assert plan["non_session"] == ["2025-12-25"]


def test_clean_series_needs_no_work():
    db = _memory_db()
    for i, day in enumerate(["2026-10-05", "2026-10-06", "2026-10-07"]):
        _bar(db, "EUNL.DE", day, 100.0 + i, "yfinance")
    db.commit()
    plan = repair.scan_symbol(db, "EUNL.DE")
    assert plan["repull"] is False and plan["non_session"] == []


def test_suspect_bars_leave_bar_prices_when_the_repull_is_refused():
    # AVB on 2026-10-12: yfinance returns a 1-bar stub, so the re-pull is
    # refused and the stored x2.8 close must still go to quarantine.
    from unittest.mock import MagicMock

    from app.foundation.data_backbone.ingest import DataIngester
    from app.foundation.models.entities import BarQuarantine

    db = _memory_db()
    closes = [65.8, 65.1, 64.4, 65.8, 184.06, 63.7, 64.0, 64.3]
    days = ["2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-17",
            "2026-08-18"]
    for day, close in zip(days, closes):
        _bar(db, "AVB", day, close, "yfinance")
    db.commit()
    plan = repair.scan_symbol(db, "AVB")
    assert plan["suspect_bars"] == [("2026-08-13", "spike_reversal")]

    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = MagicMock()
    ingester.registry.get_price_history.return_value = {
        "ok": True, "provider": "yfinance",
        "data": [{"date": "2026-08-13", "open": 184.0, "high": 184.0, "low": 184.0, "close": 184.06, "volume": 1}],
    }
    result = repair.repair_symbol(db, ingester, plan)

    assert result is not None and not result["success"] and result["quarantined"] == 1
    assert db.execute(text("SELECT count(*) FROM bar_prices WHERE symbol='AVB'")).scalar() == 7
    assert db.query(BarQuarantine).one().reason == "spike_reversal"
