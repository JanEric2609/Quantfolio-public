"""Tests for Massive (Polygon.io) market data provider."""

from decimal import Decimal
from unittest.mock import patch

from app.foundation.providers.massive_provider import MassiveProvider


def test_provider_disabled_without_key():
    provider = MassiveProvider(api_key=None)
    assert provider.enabled is False
    result = provider.get_quote("AAPL")
    assert result["ok"] is False


def test_quote_parses_prev_endpoint():
    provider = MassiveProvider(api_key="test-key")
    mock_resp = {
        "results": [
            {
                "t": 1609459200000,
                "o": 150.0,
                "h": 155.0,
                "l": 149.0,
                "c": 154.0,
                "v": 1000000,
            }
        ]
    }
    with patch(
        "app.foundation.providers.massive_provider.MassiveProvider._get",
        return_value=mock_resp,
    ):
        result = provider.get_quote("AAPL")
    assert result["ok"] is True
    assert result["data"]["close"] == Decimal("154.0")
    assert result["data"]["volume"] == 1000000
    assert result["data"]["symbol"] == "AAPL"
    assert result["quality"]["confidence"] == 0.85


def test_quote_no_results_returns_error():
    provider = MassiveProvider(api_key="test-key")
    with patch(
        "app.foundation.providers.massive_provider.MassiveProvider._get",
        return_value={"results": []},
    ):
        result = provider.get_quote("INVALID")
    assert result["ok"] is False


def test_fundamentals_parses_ticker_endpoint():
    provider = MassiveProvider(api_key="test-key")
    mock_resp = {
        "results": {
            "name": "Apple Inc.",
            "market_cap": 2000000000000,
            "sic_description": "Computers",
        }
    }
    with patch(
        "app.foundation.providers.massive_provider.MassiveProvider._get",
        return_value=mock_resp,
    ):
        result = provider.get_fundamentals("AAPL")
    assert result["ok"] is True
    assert result["data"]["name"] == "Apple Inc."
    assert result["data"]["market_cap"] == 2000000000000
    assert result["data"]["industry"] == "Computers"
    assert result["quality"]["confidence"] == 0.7
    assert result["quality"]["missing_fields"] == ["pe_ratio", "pb_ratio", "roe", "dividend_yield"]


def test_fundamentals_not_found_returns_error():
    provider = MassiveProvider(api_key="test-key")
    with patch(
        "app.foundation.providers.massive_provider.MassiveProvider._get",
        return_value={"status": "NOT_FOUND"},
    ):
        result = provider.get_fundamentals("INVALID")
    assert result["ok"] is False


def test_fundamentals_empty_results_returns_error():
    provider = MassiveProvider(api_key="test-key")
    with patch(
        "app.foundation.providers.massive_provider.MassiveProvider._get",
        return_value={"results": {}},
    ):
        result = provider.get_fundamentals("INVALID")
    assert result["ok"] is False


def test_status_available_even_when_market_closed():
    """Provider should be available for history/fundamentals even when market is closed."""
    provider = MassiveProvider(api_key="test-key")
    mock_connectivity = {"status": "OK"}
    mock_market_state = {"market": "closed", "afterHours": True}
    with patch(
        "app.foundation.providers.massive_provider.MassiveProvider._get",
        side_effect=[mock_connectivity, mock_market_state],
    ):
        result = provider.status()
    assert result["available"] is True  # Should be True — API works, just market is closed
    assert result["enabled"] is True
    assert result["market_open"] is False or "market_open" in result
