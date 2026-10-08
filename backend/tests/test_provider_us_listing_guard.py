"""Regression tests for the is_us_listing() guard (Track E0) on
FinnhubProvider, AlphaVantageProvider, and OpenBBProvider — all three
strip yfinance-style exchange suffixes (EUNL.DE -> EUNL) before querying
their backend, which risks silently matching an unrelated US ticker
(SHEL.AS -> SHEL) and returning the wrong company's data. AlpacaProvider/
DatabentoProvider already skip non-US symbols instead of stripping them;
this pins the same discipline for the other three call chains.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from app.foundation.providers.alphavantage_provider import AlphaVantageProvider
from app.foundation.providers.finnhub_provider import FinnhubProvider
from app.foundation.providers.openbb_provider import OpenBBProvider


def _boom_if_called(*_args, **_kwargs):
    raise AssertionError("HTTP client must not be called for a skipped non-US symbol")


class TestFinnhubUsListingGuard:
    def test_quote_skips_non_us_symbol_without_calling_http(self):
        provider = FinnhubProvider(api_key="k")
        with patch("app.foundation.providers.finnhub_provider.httpx.Client", side_effect=_boom_if_called):
            result = provider.get_quote("SHEL.AS")
        assert result["ok"] is False

    def test_fundamentals_skips_non_us_symbol(self):
        provider = FinnhubProvider(api_key="k")
        with patch("app.foundation.providers.finnhub_provider.httpx.Client", side_effect=_boom_if_called):
            result = provider.get_fundamentals("EUNL.DE")
        assert result["ok"] is False

    def test_news_skips_non_us_symbol(self):
        provider = FinnhubProvider(api_key="k")
        with patch("app.foundation.providers.finnhub_provider.httpx.Client", side_effect=_boom_if_called):
            result = provider.get_news("VWCE.DE")
        assert result["ok"] is False

    def test_news_without_symbol_is_unaffected(self):
        """General (non-symbol-scoped) news must not be gated by the guard."""
        provider = FinnhubProvider(api_key="k")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        with patch("app.foundation.providers.finnhub_provider.httpx.Client", return_value=mock_client):
            provider.get_news(None)
        assert mock_client.get.called

    def test_analyst_estimates_skips_non_us_symbol(self):
        provider = FinnhubProvider(api_key="k")
        with patch("app.foundation.providers.finnhub_provider.httpx.Client", side_effect=_boom_if_called):
            result = provider.get_analyst_estimates("SHEL.AS")
        assert result["ok"] is False

    def test_quote_still_works_for_us_symbol(self):
        provider = FinnhubProvider(api_key="k")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"c": 100.0, "t": 1_700_000_000}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        with patch("app.foundation.providers.finnhub_provider.httpx.Client", return_value=mock_client):
            result = provider.get_quote("AAPL")
        assert result["ok"] is True


class TestAlphaVantageUsListingGuard:
    def test_news_skips_non_us_symbol_without_calling_http(self):
        provider = AlphaVantageProvider(api_key="k")
        provider._minute_limiter.reset()
        provider._daily_limiter.reset()
        with patch("app.foundation.providers.alphavantage_provider.httpx.Client", side_effect=_boom_if_called):
            result = provider.get_news("SHEL.AS")
        assert result["ok"] is False
        assert "US listings" in (result.get("error") or "")

    def test_news_without_symbol_is_unaffected(self):
        provider = AlphaVantageProvider(api_key="k")
        # get_shared_limiter() is process-global and keyed by name — another
        # test exhausting "alphavantage/minute" earlier in the same worker
        # would otherwise make this test flaky depending on run order.
        provider._minute_limiter.reset()
        provider._daily_limiter.reset()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"feed": []}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        with patch("app.foundation.providers.alphavantage_provider.httpx.Client", return_value=mock_client):
            provider.get_news(None)
        assert mock_client.get.called


class TestOpenBBUsListingGuard:
    def test_non_suffix_aware_default_provider_skips_non_us_symbol(self):
        provider = OpenBBProvider(enabled=True, default_provider="fmp")
        with patch.object(provider, "_api_request") as mock_api:
            result = provider.get_quote("SHEL.AS")
        mock_api.assert_not_called()
        assert result["ok"] is False

    def test_yfinance_default_provider_preserves_suffix(self):
        provider = OpenBBProvider(enabled=True, default_provider="yfinance")
        with patch.object(provider, "_api_request") as mock_api:
            mock_api.return_value = {"ok": True, "data": [], "quality": {"warnings": []}}
            provider.get_quote("SHEL.AS")
        call_kwargs = mock_api.call_args[0][1]
        assert call_kwargs["symbol"] == "SHEL.AS"


@pytest.fixture(autouse=True)
def _no_shared_rate_limits(monkeypatch):
    # The registry's limiters are process-global and keyed by provider name
    # (EODHD: 20 a day); other tests in the same worker can use them up, and
    # the registry then skips the provider as throttled.
    monkeypatch.setattr("app.foundation.providers.registry.create_limiters", lambda name: [])


def _ingestion_order(symbol):
    from app.foundation.providers.base import MarketDataProvider
    from app.foundation.providers.registry import ProviderRegistry

    calls = []

    class Fake(MarketDataProvider):
        capabilities = {"get_history"}

        def __init__(self, name):
            super().__init__(enabled=True)
            self.name = name

        def get_history(self, symbol, start=None, end=None, days=None):
            calls.append(self.name)
            return {"ok": False, "error": "no"}

    names = ["yfinance", "eod", "databento", "tiingo", "twelvedata", "finnhub"]
    ProviderRegistry([Fake(n) for n in names]).get_price_history(symbol)
    return calls


def test_us_symbol_keeps_the_quant_grade_order_without_eod():
    order = _ingestion_order("AAPL")
    assert order[:3] == ["tiingo", "twelvedata", "databento"]
    assert "eod" not in order


def test_eod_is_used_only_when_asked_for_by_name():
    from app.foundation.providers.base import MarketDataProvider
    from app.foundation.providers.registry import ProviderRegistry

    calls = []

    class Fake(MarketDataProvider):
        capabilities = {"get_history"}

        def __init__(self, name):
            super().__init__(enabled=True)
            self.name = name

        def get_history(self, symbol, start=None, end=None, days=None):
            calls.append(self.name)
            return {"ok": True, "provider": self.name, "data": [], "quality": {}, "error": None}

    reg = ProviderRegistry([Fake("tiingo"), Fake("eod")])
    reg.get_price_history("AAPL", provider="eod")
    assert calls == ["eod"]


def test_non_us_symbol_skips_us_only_providers_and_eod():
    for symbol in ("EUNL.DE", "VWRA.L", "ASML.AS", "IE00B4L5Y983"):
        order = _ingestion_order(symbol)
        assert order[0] == "twelvedata", symbol
        assert not {"tiingo", "databento", "finnhub", "eod"} & set(order), symbol
        assert "yfinance" in order
