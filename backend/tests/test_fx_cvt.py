"""Tests for FX conversion service."""
from unittest.mock import patch
from app.foundation.fx_cvt import get_rate, _cache_clear
from app.foundation.fx_rates import convert


def test_get_rate_caches_result():
    """FX rate should be cached after first fetch."""
    _cache_clear()
    mock_result = {"ok": True, "data": {"base": "USD", "quote": "EUR", "rate": 0.92}}

    with patch("app.foundation.fx_cvt.build_provider_registry") as mock_reg:
        mock_reg.return_value.get_fx_rate.return_value = mock_result
        rate1 = get_rate("USD", "EUR", db=None)
        rate2 = get_rate("USD", "EUR", db=None)

        assert rate1 == 0.92
        assert rate2 == 0.92
        # Provider called only once (second hit from cache)
        mock_reg.return_value.get_fx_rate.assert_called_once()


def test_get_rate_returns_none_on_failure():
    """Failed FX lookup should return None, not raise."""
    _cache_clear()
    with patch("app.foundation.fx_cvt.build_provider_registry") as mock_reg:
        mock_reg.return_value.get_fx_rate.return_value = {"ok": False, "data": None}
        rate = get_rate("XYZ", "EUR", db=None)
        assert rate is None


def test_convert_same_currency():
    """Converting EUR→EUR should return the same amount."""
    result = convert(100.0, "EUR", "EUR", db=None)
    assert result == 100.0


def test_convert_uses_rate():
    """Conversion should multiply amount by rate."""
    _cache_clear()
    mock_result = {"ok": True, "data": {"base": "USD", "quote": "EUR", "rate": 0.92}}

    with patch("app.foundation.fx_cvt.build_provider_registry") as mock_reg:
        mock_reg.return_value.get_fx_rate.return_value = mock_result
        result = convert(100.0, "USD", "EUR", db=None)
        assert abs(result - 92.0) < 0.01
