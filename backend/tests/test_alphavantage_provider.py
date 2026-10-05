"""Tests for Alpha Vantage provider rate limiting."""
from unittest.mock import patch

from app.foundation.providers.alphavantage_provider import AlphaVantageProvider


def test_get_quote_returns_unavailable_when_throttled():
    provider = AlphaVantageProvider(api_key="test-key")
    # Exhaust the rate limit
    for _ in range(5):
        provider._minute_limiter.acquire()
    with patch.object(provider, "_get", return_value={}) as mock_get:
        result = provider.get_quote("AAPL")
    assert result["ok"] is False
    error = result.get("error") or ""
    assert "rate limit" in error.lower() or "throttl" in error.lower()
    mock_get.assert_not_called()
