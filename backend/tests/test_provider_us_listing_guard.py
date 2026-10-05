"""Regression tests for the is_us_listing() guard (Track E0) on
FinnhubProvider, AlphaVantageProvider, and OpenBBProvider — all three
strip yfinance-style exchange suffixes (EUNL.DE -> EUNL) before querying
their backend, which risks silently matching an unrelated US ticker
(SHEL.AS -> SHEL) and returning the wrong company's data. AlpacaProvider/
DatabentoProvider already skip non-US symbols instead of stripping them;
this pins the same discipline for the other three call chains.
"""
from __future__ import annotations

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
