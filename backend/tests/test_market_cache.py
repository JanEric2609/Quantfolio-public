"""Tests for the price-cache write guard in ``services.market``.

Providers can return a NaN/None close for the latest, not-yet-settled day.
``price_cache.close`` is NOT NULL (and SQLite stores NaN as NULL), so such a
bar must never be cached. These tests pin that behaviour.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import PriceCache
from app.foundation import market


def test_valid_close_predicate():
    assert market._is_valid_close(123.45) is True
    assert market._is_valid_close(Decimal("100")) is True
    assert market._is_valid_close(None) is False
    assert market._is_valid_close(float("nan")) is False
    assert market._is_valid_close(float("inf")) is False


def test_upsert_skips_nan_close_without_raising():
    db = _memory_db()
    market._upsert_price(
        db,
        "URTH",
        {"date": date(2026, 6, 15), "close": float("nan"), "currency": "EUR"},
    )
    db.commit()
    assert db.query(PriceCache).count() == 0


def test_upsert_persists_valid_close():
    db = _memory_db()
    market._upsert_price(
        db,
        "URTH",
        {"date": date(2026, 6, 12), "close": 200.97, "currency": "EUR"},
    )
    db.commit()
    rows = db.query(PriceCache).all()
    assert len(rows) == 1
    assert float(rows[0].close) == 200.97


# ---------------------------------------------------------------------------
# Additional edge-case tests


def test_is_valid_close_negative_infinity_is_invalid():
    assert market._is_valid_close(float("-inf")) is False


def test_is_valid_close_zero_is_valid():
    """A close of zero is technically finite and must pass the guard."""
    assert market._is_valid_close(0.0) is True


def test_is_valid_close_negative_price_is_valid():
    """Negative prices can appear for adjusted series (splits); they are finite."""
    assert market._is_valid_close(-5.0) is True


def test_is_valid_close_string_is_invalid():
    """Non-numeric strings cannot be cast to float → False."""
    assert market._is_valid_close("not-a-number") is False


def test_is_valid_close_numeric_string_is_valid():
    """A string that represents a valid number should be accepted."""
    assert market._is_valid_close("123.45") is True


def test_upsert_skips_none_close_without_raising():
    db = _memory_db()
    market._upsert_price(
        db,
        "SPY",
        {"date": date(2026, 6, 15), "close": None, "currency": "USD"},
    )
    db.commit()
    assert db.query(PriceCache).count() == 0


def test_upsert_skips_negative_infinity_close():
    db = _memory_db()
    market._upsert_price(
        db,
        "QQQ",
        {"date": date(2026, 6, 15), "close": float("-inf"), "currency": "USD"},
    )
    db.commit()
    assert db.query(PriceCache).count() == 0


def test_upsert_idempotent_same_bar_twice():
    """Calling _upsert_price for the same ticker+date produces exactly one row."""
    db = _memory_db()
    bar = {"date": date(2026, 6, 10), "close": 100.0, "currency": "USD"}
    market._upsert_price(db, "AAPL", bar)
    market._upsert_price(db, "AAPL", bar)
    db.commit()
    assert db.query(PriceCache).count() == 1


def test_upsert_updates_close_on_second_call():
    """A second upsert for the same bar should overwrite the close price."""
    db = _memory_db()
    d = date(2026, 6, 10)
    market._upsert_price(db, "MSFT", {"date": d, "close": 300.0, "currency": "USD"})
    db.commit()

    market._upsert_price(db, "MSFT", {"date": d, "close": 305.0, "currency": "USD"})
    db.commit()

    rows = db.query(PriceCache).all()
    assert len(rows) == 1
    assert float(rows[0].close) == 305.0


def test_upsert_commit_false_does_not_flush_to_db():
    """With commit=False the row is pending but the DB count is still 0 mid-flight."""
    db = _memory_db()
    market._upsert_price(
        db,
        "NVDA",
        {"date": date(2026, 6, 11), "close": 900.0, "currency": "USD"},
        commit=False,
    )
    # Without an explicit commit the record should not be visible to a fresh query.
    db.rollback()
    assert db.query(PriceCache).count() == 0
