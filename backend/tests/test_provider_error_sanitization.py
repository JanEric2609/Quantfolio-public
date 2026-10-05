"""Verify that market-data provider adapters never leak API keys/tokens into
error messages when the upstream HTTP call returns a 4xx/5xx status.

Historically several providers called ``response.raise_for_status()`` and let
httpx's ``HTTPStatusError`` propagate unmodified. That exception's message
embeds the *full request URL*, including any api key/token sent as a query
parameter — and that string flows into ``provider_result(... error=str(exc))``,
which is logged and can be persisted into ``ProviderHealth.meta_json`` and
returned from the provider "test connection" API. Each test below simulates
an upstream error response and asserts the secret value used in the request
never appears anywhere in the resulting provider_result payload.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.foundation.providers.alpaca_provider import AlpacaProvider
from app.foundation.providers.alphavantage_provider import AlphaVantageProvider
from app.foundation.providers.databento_provider import DatabentoProvider
from app.foundation.providers.eod_provider import EodProvider
from app.foundation.providers.finnhub_provider import FinnhubProvider
from app.foundation.providers.tiingo_provider import TiingoProvider

SECRET = "SECRET-TOKEN-DO-NOT-LEAK-12345"


def _mock_client(status_code: int = 500):
    """Return a context-manager-compatible mock httpx.Client whose .get()/.post()
    return an error response. .json() is intentionally left unconfigured — the
    sanitized _get() implementations must raise before ever calling it."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.side_effect = AssertionError(
        "response.json() must not be called after a 4xx/5xx status"
    )

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_response
    mock_client.post.return_value = mock_response
    return mock_client


def _assert_no_secret_leak(result: dict) -> None:
    assert result["ok"] is False
    dumped = json.dumps(result, default=str)
    assert SECRET not in dumped, f"secret leaked into provider_result: {dumped}"


def test_finnhub_error_does_not_leak_token():
    provider = FinnhubProvider(api_key=SECRET)
    with patch("app.foundation.providers.finnhub_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(401)
        result = provider.get_quote("AAPL")
    _assert_no_secret_leak(result)


def test_alphavantage_error_does_not_leak_apikey():
    provider = AlphaVantageProvider(api_key=SECRET)
    with patch("app.foundation.providers.alphavantage_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(403)
        result = provider.get_quote("AAPL")
    _assert_no_secret_leak(result)


def test_eod_history_error_does_not_leak_api_token():
    provider = EodProvider(api_key=SECRET)
    with patch("app.foundation.providers.eod_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(402)
        result = provider.get_history("AAPL.US")
    _assert_no_secret_leak(result)


def test_eod_fundamentals_error_does_not_leak_api_token():
    provider = EodProvider(api_key=SECRET)
    with patch("app.foundation.providers.eod_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(500)
        result = provider.get_fundamentals("AAPL.US")
    _assert_no_secret_leak(result)


def test_eod_quote_error_does_not_leak_api_token():
    provider = EodProvider(api_key=SECRET)
    with patch("app.foundation.providers.eod_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(429)
        result = provider.get_quote("AAPL.US")
    _assert_no_secret_leak(result)


def test_databento_error_does_not_leak_api_key():
    provider = DatabentoProvider(api_key=SECRET)
    with patch("app.foundation.providers.databento_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(401)
        result = provider.get_quote("AAPL")
    _assert_no_secret_leak(result)


def test_tiingo_error_does_not_leak_token():
    provider = TiingoProvider(api_key=SECRET)
    with patch("app.foundation.providers.tiingo_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(404)
        result = provider.get_quote("AAPL")
    _assert_no_secret_leak(result)


def test_alpaca_error_does_not_leak_key_or_secret():
    provider = AlpacaProvider(api_key=SECRET, api_secret="ANOTHER-SECRET-VALUE")
    with patch("app.foundation.providers.alpaca_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(401)
        result = provider.get_quote("AAPL")
    _assert_no_secret_leak(result)
    assert "ANOTHER-SECRET-VALUE" not in json.dumps(result, default=str)
