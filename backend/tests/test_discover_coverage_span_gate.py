"""M1 span/length gate tests for stage_history_ingest (audit finding F4).

The old coverage-only check measured coverage against *returned* rows, so a
gapless-but-shallow series (e.g. 100 recent bars) passed with fake "100%
coverage" and produced silently wrong annualized CAGRs downstream. These tests
pin the new gates:

- span: first bar must reach back to ``today - requested_days`` (+ tolerance);
- scaled length floor: ``max(250, ceil(0.55 * 252 * days/365))`` (694 for 5y);
- young listings (~2y+) pass WITH a ``short_history`` concern, never rejected;
- ``data_gap`` is a concern-only flag (median spacing / single-hole rules);
- the cached path enforces the identical gate — no bypass via warm cache.

All PriceCache seeding follows tests/test_discover_pipeline_fixes.py:_seed_prices
(business-day walk, Decimal closes, fetched_at now, source="test"); only the
external provider ingester is mocked — the DB layer stays real.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import PriceCache
from app.foundation.data_backbone.ingest import DataIngester
from app.decision.discover.pipeline import stage_history_ingest


# ---------------------------------------------------------------------------
# Seeding helpers (PriceCache pattern from test_discover_pipeline_fixes.py)
# ---------------------------------------------------------------------------


def _seed_rows(db, ticker: str, dates: list[date], closes: list[float]) -> None:
    """Seed PriceCache rows for explicit dates (ascending), Decimal closes."""
    now = datetime.now(UTC)
    for dd, close in zip(dates, closes):
        db.add(PriceCache(
            id=uuid4().hex, ticker=ticker.upper(), date=dd,
            close=Decimal(str(round(close, 4))), fetched_at=now,
            source="test", stale=False, currency="EUR",
        ))
    db.commit()


def _business_days_back(n: int, end: date | None = None) -> list[date]:
    """*n* business days ending at *end* (yesterday by default), ascending."""
    d = end or (date.today() - timedelta(days=1))
    days: list[date] = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _business_days_between(start: date, end: date) -> list[date]:
    """Business days in [start, end], ascending."""
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _mock_ingest_failure(monkeypatch, message: str = "provider has no history") -> None:
    """Simulate a provider chain that cannot supply any history (no network)."""

    def _fail(self, symbol, **kwargs):
        return {"success": False, "message": message}

    monkeypatch.setattr(DataIngester, "ingest_bar_prices", _fail)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_shallow_100_bar_series_rejected_with_reason(monkeypatch):
    """THE acceptance test: a gapless 100-bar series must be rejected with an
    explicit machine-parseable reason mentioning span/length/closes."""
    db = _memory_db()
    _mock_ingest_failure(monkeypatch)
    dates = _business_days_back(100)
    _seed_rows(db, "SHALLOW", dates, [100.0 + i * 0.01 for i in range(100)])

    scores, error = stage_history_ingest(db, "SHALLOW")

    assert scores is None
    assert error is not None
    assert error.startswith("history_ingest: ")
    assert "span" in error or "length" in error or "closes" in error


def test_full_span_series_passes(monkeypatch):
    """A series spanning the full 5y window passes unchanged — no concerns."""
    db = _memory_db()
    _mock_ingest_failure(monkeypatch)
    # ~1330 business days ≈ 1862 calendar days: first bar lands safely before
    # the 5y cutoff (+10d tolerance); 1250 bars would stop ~65 days short of it.
    n = 1330
    dates = _business_days_back(n)
    _seed_rows(db, "FULL", dates, [100.0 + i * 0.01 for i in range(n)])

    scores, error = stage_history_ingest(db, "FULL")

    assert error is None
    assert scores is not None
    assert scores["bars"] >= 700
    assert scores["concerns"] == []


def test_young_listing_passes_with_short_history_concern(monkeypatch):
    """A legit recent listing (~2.5y of history) passes WITH a short_history
    concern — policy LOCKED: flag, never reject (audit F4 decision)."""
    db = _memory_db()
    _mock_ingest_failure(monkeypatch)
    first = date.today() - timedelta(days=900)  # ~2.46 years ago
    dates = _business_days_between(first, date.today() - timedelta(days=1))
    assert len(dates) >= 250  # sanity: young but substantial
    _seed_rows(db, "YOUNG", dates, [50.0 + i * 0.02 for i in range(len(dates))])

    scores, error = stage_history_ingest(db, "YOUNG")

    assert error is None
    assert scores is not None
    assert "short_history" in scores["concerns"]


def test_cached_path_enforces_same_gate_no_bypass(monkeypatch):
    """Shallow cache rows + provider failure → still rejected with a span/closes
    reason. A warm cache alone can never bless shallow data (identical gate on
    both paths makes the bypass impossible)."""
    db = _memory_db()
    _mock_ingest_failure(monkeypatch, message="provider has no history")
    dates = _business_days_back(100)
    _seed_rows(db, "CACHEY", dates, [100.0 + i * 0.01 for i in range(100)])

    scores, error = stage_history_ingest(db, "CACHEY")

    assert scores is None
    assert error is not None
    assert error.startswith("history_ingest: ")
    assert "span" in error or "length" in error or "closes" in error


def test_data_gap_concern_on_holey_series(monkeypatch):
    """Full-span full-length series with one contiguous 20-calendar-day hole
    passes, but carries a data_gap concern (concern-only, never rejects)."""
    db = _memory_db()
    _mock_ingest_failure(monkeypatch)
    n = 1330
    dates = _business_days_back(n)
    mid = dates[len(dates) // 2]
    holed = [d for d in dates if not (mid <= d < mid + timedelta(days=20))]
    _seed_rows(db, "HOLEY", holed, [100.0 + i * 0.01 for i in range(len(holed))])

    scores, error = stage_history_ingest(db, "HOLEY")

    assert error is None
    assert scores is not None
    assert "data_gap" in scores["concerns"]


def test_insufficient_length_rejected_when_span_claims_full(monkeypatch):
    """Sparse vendor data: first_date claims a 5y anchor but only 500 closes
    exist across the window → REJECT with the insufficient-length message
    (span ok, length fails — the sparse-hole catch)."""
    db = _memory_db()
    _mock_ingest_failure(monkeypatch)
    start = date.today() - timedelta(days=1900)  # beyond the 5y cutoff
    end = date.today() - timedelta(days=1)
    step = (end - start) / 499
    dates = [(start + step * i) for i in range(500)]
    dates = [d.date() if isinstance(d, datetime) else d for d in dates]
    _seed_rows(db, "SPARSE", dates, [100.0 + i * 0.01 for i in range(500)])

    scores, error = stage_history_ingest(db, "SPARSE")

    assert scores is None
    assert error is not None
    assert error.startswith("history_ingest: ")
    assert "insufficient length" in error
    # market_history clips rows to the requested window, so the stage sees the
    # post-cutoff bar count; pin the scaled floor value instead of the count.
    assert "< 694" in error
