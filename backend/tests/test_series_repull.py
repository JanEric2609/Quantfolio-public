"""Weekly full re-pull of stored bar series (plan 2026-10-12, D1)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from conftest import _memory_db
from sqlalchemy import text

from app.foundation.data_backbone import series_repull
from app.foundation.models.entities import BarSeriesMeta

NOW = datetime(2026, 10, 18, 5, 0, tzinfo=UTC)


def _bar(db, symbol: str) -> None:
    db.execute(text(
        "INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider) "
        "VALUES (:s, '2026-10-16 00:00:00', 1, 1, 1, 1, 1, 'USD', 'yfinance')"
    ), {"s": symbol})


def test_due_symbols_skip_recent_repulls_and_put_the_oldest_first():
    db = _memory_db()
    for symbol in ("FRESH", "OLD", "OLDER", "NEVER"):
        _bar(db, symbol)
    db.add_all([
        BarSeriesMeta(symbol="FRESH", provider="yfinance", last_full_repull_at=NOW - timedelta(days=2)),
        BarSeriesMeta(symbol="OLD", provider="yfinance", last_full_repull_at=NOW - timedelta(days=8)),
        BarSeriesMeta(symbol="OLDER", provider="yfinance", last_full_repull_at=NOW - timedelta(days=20)),
    ])
    db.commit()
    assert series_repull.due_symbols(db, now=NOW) == ["NEVER", "OLDER", "OLD"]


def test_the_weekly_cap_covers_the_stored_universe():
    # 851 series were stored on 2026-10-12; each must be re-pulled every week.
    assert series_repull.REPULL_MAX_SYMBOLS >= 851


def test_one_failing_series_does_not_stop_the_run():
    db = _memory_db()
    for symbol in ("A", "B", "C"):
        _bar(db, symbol)
    db.commit()
    outcomes = {"A": {"success": True}, "B": {"success": False}, "C": {"success": True, "split_unadjusted": True}}

    class FakeIngester:
        def __init__(self, _db):
            self.registry = type("R", (), {"providers": []})()

        def repull_series(self, symbol, provider=None):
            return outcomes[symbol]

    with patch("app.foundation.data_backbone.ingest.DataIngester", FakeIngester):
        result = series_repull.repull_due_series(db)
    assert result == {"repulled": 2, "failed": ["B"], "split_unadjusted": ["C"]}
