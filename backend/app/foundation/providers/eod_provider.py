"""EOD Historical Data provider for deep equity history."""

from datetime import datetime
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.rate_limiter import RateLimitConfig, get_shared_limiter

# EODHD's exchange codes mostly match yfinance's suffixes (.PA, .MC, .AS, .BR,
# .CO, .ST, .HE, .VI, .LS, .IR, .TO all pass through unchanged, and a bare US
# symbol defaults to US), but Xetra/Germany is the one confirmed mismatch:
# yfinance uses ".DE", EODHD uses ".XETRA" — an unmapped ".DE" symbol 404s
# against this provider. (docs: https://eodhd.com/list-of-stock-markets)
_SUFFIX_REMAP = {"DE": "XETRA"}


def _to_eodhd_symbol(symbol: str) -> str:
    if "." not in symbol:
        return symbol
    base, suffix = symbol.rsplit(".", 1)
    remapped = _SUFFIX_REMAP.get(suffix.upper())
    return f"{base}.{remapped}" if remapped else symbol


class EodProvider(MarketDataProvider):
    """EOD Historical Data provider for equity OHLCV and fundamentals."""

    name = "eod"
    capabilities = {"get_history", "get_fundamentals", "get_quote"}
    base_url = "https://eodhd.com/api"

    def __init__(self, api_key: str | None = None, *, enabled: bool = True):
        super().__init__(enabled=enabled)
        self.api_key = api_key
        self._enabled = enabled and api_key is not None
        # Free tier: 20 calls/day
        self._daily_limiter = get_shared_limiter("eod/daily", RateLimitConfig(max_calls=20, window_seconds=86400, description="EODHD free tier: 20/day"))

    def _check_rate_limit(self) -> str | None:
        """Check rate limits. Returns error message if throttled, None if allowed."""
        if not self._daily_limiter.acquire():
            return "EODHD daily rate limit exceeded (20/day)."
        return None

    def _get(self, endpoint: str, symbol: str, params: dict[str, Any]) -> Any:
        """GET a JSON endpoint from EOD Historical Data, sanitizing HTTP errors.

        Never re-raise httpx's default error: its message embeds the full
        request URL including the api_token, which callers log and store in
        ``ProviderHealth.meta_json``.
        """
        eodhd_symbol = _to_eodhd_symbol(symbol)
        url = f"{self.base_url}/{endpoint}/{eodhd_symbol}"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code >= 400:
                raise ValueError(f"EOD Historical Data HTTP {resp.status_code} for {endpoint}/{eodhd_symbol}")
            return resp.json()

    def status(self) -> dict[str, Any]:
        base = super().status()
        if not self.api_key:
            base["available"] = False
            base["message"] = "EOD Historical Data API key not configured."
        elif not self._enabled:
            base["available"] = False
            base["message"] = "EOD Historical Data provider is disabled."
        else:
            base["available"] = True
            base["message"] = "EOD Historical Data provider is configured."
        return base

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """Fetch historical OHLCV from EOD Historical Data.

        Args:
            symbol: Ticker with exchange suffix (e.g., 'AAPL.US', 'SAP.DE').
            start: ISO format start date.
            end: ISO format end date.
            days: Number of days to fetch from today.
        """
        if not self._enabled:
            return self.unavailable("history", "EOD Historical Data API key not configured")
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("history", rate_error)

        try:
            params = {"api_token": self.api_key, "fmt": "json"}
            if start:
                params["from"] = start
            if end:
                params["to"] = end

            data = self._get("eod", symbol, params)

            if not isinstance(data, list):
                return provider_result(
                    self.name,
                    ok=False,
                    error="Unexpected response format from EOD Historical Data",
                )

            if len(data) == 0:
                return provider_result(
                    self.name,
                    ok=False,
                    error=f"No data found for {symbol}",
                )

            # Transform to standard format
            records = [
                {
                    "date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "adjusted_close": row.get("adjusted_close"),
                    "volume": row.get("volume"),
                }
                for row in data
            ]

            return provider_result(
                self.name,
                ok=True,
                data=records,
                as_of=datetime.now(),
                confidence=0.98,
            )

        except Exception as e:
            return provider_result(
                self.name,
                ok=False,
                error=str(e),
                warnings=[f"EOD Historical Data fetch failed: {str(e)}"],
            )

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Fetch fundamentals (company info + financials) from EOD Historical Data."""
        if not self._enabled:
            return self.unavailable(
                "fundamentals", "EOD Historical Data API key not configured"
            )
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("fundamentals", rate_error)

        try:
            params = {"api_token": self.api_key}
            data = self._get("fundamentals", symbol, params)

            return provider_result(
                self.name,
                ok=True,
                data=data,
                as_of=datetime.now(),
                confidence=0.95,
            )

        except Exception as e:
            return provider_result(
                self.name,
                ok=False,
                error=str(e),
                warnings=[f"EOD fundamentals fetch failed: {str(e)}"],
            )

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Fetch real-time quote from EOD Historical Data."""
        if not self._enabled:
            return self.unavailable(
                "quote", "EOD Historical Data API key not configured"
            )
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("quote", rate_error)

        try:
            params = {"api_token": self.api_key, "fmt": "json"}
            data = self._get("real-time", symbol, params)

            if "error" in data:
                return provider_result(
                    self.name,
                    ok=False,
                    error=data.get("error"),
                )

            return provider_result(
                self.name,
                ok=True,
                data={
                    "symbol": data.get("code"),
                    "price": data.get("close"),
                    "timestamp": data.get("timestamp"),
                },
                as_of=datetime.now(),
                confidence=0.99,
            )

        except Exception as e:
            return provider_result(
                self.name,
                ok=False,
                error=str(e),
                warnings=[f"EOD quote fetch failed: {str(e)}"],
            )
