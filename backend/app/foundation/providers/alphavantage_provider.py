from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.rate_limiter import RateLimitConfig, get_shared_limiter
from app.foundation.providers.utils import is_us_listing, strip_exchange_suffix


class AlphaVantageProvider(MarketDataProvider):
    name = "alphavantage"
    capabilities = {"get_quote", "get_history", "get_fundamentals", "get_news", "get_fx_rate"}

    def __init__(self, api_key: str | None, *, enabled: bool = True):
        super().__init__(enabled=enabled and bool(api_key))
        self.api_key = api_key
        # Free tier: 5 calls/min, 25 calls/day
        self._minute_limiter = get_shared_limiter("alphavantage/minute", RateLimitConfig(max_calls=5, window_seconds=60, description="Alpha Vantage free tier: 5/min"))
        self._daily_limiter = get_shared_limiter("alphavantage/daily", RateLimitConfig(max_calls=25, window_seconds=86400, description="Alpha Vantage free tier: 25/day"))
        self.base_url = "https://www.alphavantage.co/query"

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": bool(self.api_key),
            "message": "API key configured." if self.api_key else "Alpha Vantage API key is not configured.",
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("quote", "Alpha Vantage API key is not configured.")
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("quote", rate_error)
        try:
            payload = self._get({"function": "GLOBAL_QUOTE", "symbol": symbol})
            data = payload.get("Global Quote", {})
            if not data:
                return self._api_warning(payload, "quote")
            close = data.get("05. price")
            day = data.get("07. latest trading day")
            return provider_result(
                self.name,
                ok=bool(close),
                data={
                    "symbol": symbol.upper(),
                    "date": datetime.fromisoformat(day).date() if day else datetime.now(UTC).date(),
                    "open": _decimal(data.get("02. open")),
                    "high": _decimal(data.get("03. high")),
                    "low": _decimal(data.get("04. low")),
                    "close": _decimal(close),
                    "volume": _decimal(data.get("06. volume")),
                    "source": self.name,
                },
                as_of=datetime.now(UTC),
                missing_fields=[key for key, value in {"close": close, "latest_trading_day": day}.items() if not value],
                confidence=0.72,
            )
        except Exception as exc:
            return self.unavailable("quote", str(exc))

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("history", "Alpha Vantage API key is not configured.")
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("history", rate_error)
        try:
            payload = self._get({"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol, "outputsize": "compact"})
            series = payload.get("Time Series (Daily)", {})
            if not series:
                return self._api_warning(payload, "history")
            rows = []
            start_day = datetime.fromisoformat(start).date() if start else None
            end_day = datetime.fromisoformat(end).date() if end else None
            for day, item in sorted(series.items()):
                parsed = datetime.fromisoformat(day).date()
                if start_day and parsed < start_day:
                    continue
                if end_day and parsed > end_day:
                    continue
                rows.append(
                    {
                        "date": parsed,
                        "open": _decimal(item.get("1. open")),
                        "high": _decimal(item.get("2. high")),
                        "low": _decimal(item.get("3. low")),
                        "close": _decimal(item.get("5. adjusted close") or item.get("4. close")),
                        "volume": _decimal(item.get("6. volume")),
                        "source": self.name,
                    }
                )
            if days and len(rows) > days:
                rows = rows[-days:]
            return provider_result(
                self.name,
                ok=bool(rows),
                data=rows,
                as_of=datetime.now(UTC),
                confidence=0.74 if len(rows) >= 252 else 0.55,
                warnings=[] if len(rows) >= 252 else ["Less than one year of Alpha Vantage history is available."],
            )
        except Exception as exc:
            return self.unavailable("history", str(exc))

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("fundamentals", "Alpha Vantage API key is not configured.")
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("fundamentals", rate_error)
        try:
            overview = self._get({"function": "OVERVIEW", "symbol": symbol})
            if not overview or "Symbol" not in overview:
                return self._api_warning(overview, "fundamentals")
            data = {
                "pe_ratio": _float(overview.get("PERatio")),
                "pb_ratio": _float(overview.get("PriceToBookRatio")),
                "ev_ebitda": _float(overview.get("EVToEBITDA")),
                "roe": _float(overview.get("ReturnOnEquityTTM")),
                "profit_margin": _float(overview.get("ProfitMargin")),
                "debt_equity": _float(overview.get("DebtToEquityRatio")),
                "revenue_growth": _float(overview.get("QuarterlyRevenueGrowthYOY")),
                "earnings_growth": _float(overview.get("QuarterlyEarningsGrowthYOY")),
                "market_cap": _float(overview.get("MarketCapitalization")),
                "sector": overview.get("Sector"),
                "industry": overview.get("Industry"),
                "currency": overview.get("Currency"),
                "beta": _float(overview.get("Beta")),
            }
            return provider_result(
                self.name,
                ok=True,
                data=data,
                as_of=datetime.now(UTC),
                missing_fields=[key for key, value in data.items() if value in (None, "")],
                confidence=0.75,
            )
        except Exception as exc:
            return self.unavailable("fundamentals", str(exc))

    def get_news(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("news", "Alpha Vantage API key is not configured.")
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("news", rate_error)
        if symbol and not is_us_listing(symbol):
            # Stripping the suffix risks matching an unrelated US ticker
            # (SHEL.AS → SHEL) and returning the wrong company's news.
            return self.unavailable("news", f"Alpha Vantage covers US listings only; skipping {symbol}.")
        try:
            params = {"function": "NEWS_SENTIMENT", "limit": str(limit)}
            if symbol:
                # Alpha Vantage does not understand yfinance-style exchange suffixes
                params["tickers"] = strip_exchange_suffix(symbol)
            payload = self._get(params)
            feed = payload.get("feed", [])
            rows = [
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "source": item.get("source"),
                    "summary": item.get("summary"),
                    "sentiment_label": item.get("overall_sentiment_label"),
                    "sentiment_score": _float(item.get("overall_sentiment_score")),
                    "published_at": item.get("time_published"),
                }
                for item in feed[:limit]
            ]
            return provider_result(self.name, ok=bool(rows), data=rows, as_of=datetime.now(UTC), confidence=0.65)
        except Exception as exc:
            return self.unavailable("news", str(exc))

    def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("FX rate", "Alpha Vantage API key is not configured.")
        rate_error = self._check_rate_limit()
        if rate_error:
            return self.unavailable("FX rate", rate_error)
        try:
            payload = self._get({"function": "CURRENCY_EXCHANGE_RATE", "from_currency": base, "to_currency": quote})
            data = payload.get("Realtime Currency Exchange Rate", {})
            rate = _float(data.get("5. Exchange Rate"))
            return provider_result(
                self.name,
                ok=rate is not None,
                data={"base": base.upper(), "quote": quote.upper(), "rate": rate},
                as_of=datetime.now(UTC),
                confidence=0.72,
            )
        except Exception as exc:
            return self.unavailable("FX rate", str(exc))

    def _get(self, params: dict[str, str]) -> dict[str, Any]:
        with httpx.Client(timeout=12) as client:
            response = client.get(self.base_url, params={**params, "apikey": self.api_key})
            if response.status_code >= 400:
                # Never re-raise httpx's default error: its message embeds the
                # full request URL including the apikey, which callers log.
                raise ValueError(
                    f"Alpha Vantage HTTP {response.status_code} for function={params.get('function', '-')} "
                    f"(symbol={params.get('symbol', '-')})"
                )
            return response.json()

    def _check_rate_limit(self) -> str | None:
        """Check rate limits. Returns error message if throttled, None if allowed."""
        if not self._minute_limiter.acquire():
            return "Alpha Vantage per-minute rate limit exceeded (5/min)."
        if not self._daily_limiter.acquire():
            return "Alpha Vantage daily rate limit exceeded (25/day)."
        return None

    def _api_warning(self, payload: dict[str, Any], capability: str) -> dict[str, Any]:
        message = payload.get("Note") or payload.get("Information") or payload.get("Error Message") or f"Alpha Vantage returned no {capability} data."
        return provider_result(self.name, ok=False, warnings=[message], error=message)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
