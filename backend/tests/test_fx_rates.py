"""Tests for the dated FX rate provider and convert(as_of=) (proposal P1)."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from conftest import _memory_db

from app.foundation.models.entities import FxRate
from app.foundation import fx_rates
from app.foundation.fx_cvt import FxRateUnavailable, _cache_clear
from app.foundation.fx_rates import convert


def _seed(db, base, quote, d, rate):
    db.add(FxRate(base=base, quote=quote, date=d, rate=Decimal(str(rate))))
    db.commit()


def test_get_rate_identity_is_one():
    db = _memory_db()
    assert fx_rates.FxRateProvider.get_rate(db, "EUR", "EUR", date(2026, 1, 2)) == 1.0


def test_get_rate_returns_stored_dated_rate():
    db = _memory_db()
    _seed(db, "USD", "EUR", date(2026, 1, 2), 0.9)
    rate = fx_rates.FxRateProvider.get_rate(db, "USD", "EUR", date(2026, 1, 2))
    assert rate == pytest.approx(0.9)


def test_get_rate_carries_forward_last_known_rate():
    db = _memory_db()
    _seed(db, "USD", "EUR", date(2026, 1, 2), 0.9)
    # No row on the 5th; a past valuation date should use the last known rate.
    rate = fx_rates.FxRateProvider.get_rate(db, "USD", "EUR", date(2026, 1, 5))
    assert rate == pytest.approx(0.9)


def test_get_rate_fetches_and_caches_spot_for_today():
    db = _memory_db()
    today = datetime.now(UTC).date()
    _cache_clear()
    with patch("app.foundation.fx_rates._spot_rate", return_value=0.8):
        rate = fx_rates.FxRateProvider.get_rate(db, "USD", "EUR", today)
    assert rate == pytest.approx(0.8)
    # It should have been persisted to the table for reuse.
    row = db.query(FxRate).filter_by(base="USD", quote="EUR", date=today).first()
    assert row is not None
    assert float(row.rate) == pytest.approx(0.8)


def test_get_rate_returns_none_when_unavailable_for_past_date():
    db = _memory_db()
    old = datetime.now(UTC).date() - timedelta(days=400)
    assert fx_rates.FxRateProvider.get_rate(db, "USD", "EUR", old) is None


def test_get_rate_inverts_when_only_reverse_pair_stored():
    db = _memory_db()
    _seed(db, "EUR", "USD", date(2026, 1, 2), 1.25)
    rate = fx_rates.FxRateProvider.get_rate(db, "USD", "EUR", date(2026, 1, 2))
    assert rate == pytest.approx(1 / 1.25)


def test_convert_with_as_of_uses_dated_rate():
    db = _memory_db()
    _seed(db, "USD", "EUR", date(2026, 1, 2), 0.9)
    out = convert(100.0, "USD", "EUR", db, as_of=date(2026, 1, 2))
    assert out == pytest.approx(90.0)


def test_convert_strict_raises_when_dated_rate_missing():
    db = _memory_db()
    old = datetime.now(UTC).date() - timedelta(days=400)
    with pytest.raises(FxRateUnavailable):
        convert(100.0, "USD", "EUR", db, strict=True, as_of=old)


def test_convert_as_of_identity_short_circuits():
    db = _memory_db()
    assert convert(100.0, "EUR", "EUR", db, as_of=date(2026, 1, 2)) == 100.0
