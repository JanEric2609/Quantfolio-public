from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.utils import is_us_listing, strip_exchange_suffix


class FinnhubProvider(MarketDataProvider):
    name = "finnhub"
    capabilities = {
        "get_quote", "get_fundamentals", "get_news", "get_analyst_estimates", "get_earnings_calendar",
    }

    def __init__(self, api_key: str | None, *, enabled: bool = True):
        super().__init__(enabled=enabled and bool(api_key))
        self.api_key = api_key
        self.base_url = "https://finnhub.io/api/v1"

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": bool(self.api_key),
            "message": "API key configured." if self.api_key else "Finnhub API key is not configured.",
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("quote", "Finnhub API key is not configured.")
        if not is_us_listing(symbol):
            # Stripping the suffix risks matching an unrelated US ticker
            # (SHEL.AS → SHEL) and returning the wrong price.
            return self.unavailable("quote", f"Finnhub covers US listings only; skipping {symbol}.")
        try:
            normalized = strip_exchange_suffix(symbol)
            data = self._get("/quote", {"symbol": normalized})
            close = data.get("c")
            timestamp = data.get("t")
            as_of = datetime.fromtimestamp(timestamp, UTC) if timestamp else datetime.now(UTC)
            return provider_result(
                self.name,
                ok=bool(close),
                data={
                    "symbol": symbol.upper(),
                    "date": as_of.date(),
                    "open": Decimal(str(data.get("o") or close)),
                    "high": Decimal(str(data.get("h") or close)),
                    "low": Decimal(str(data.get("l") or close)),
                    "close": Decimal(str(close)),
                    "volume": None,
                    "source": self.name,
                },
                as_of=as_of,
                confidence=0.7,
            )
        except Exception as exc:
            return self.unavailable("quote", str(exc))

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("fundamentals", "Finnhub API key is not configured.")
        if not is_us_listing(symbol):
            return self.unavailable("fundamentals", f"Finnhub covers US listings only; skipping {symbol}.")
        try:
            normalized = strip_exchange_suffix(symbol)
            metric = self._get("/stock/metric", {"symbol": normalized, "metric": "all"}).get("metric", {})
            profile = self._get("/stock/profile2", {"symbol": normalized})
            # Finnhub reports ratios in percent, market cap in millions and
            # volumes in millions of shares. Every consumer (the universe's
            # EUR market-cap and volume floors, the fundamentals score,
            # Piotroski, the blocks estimator) reads yfinance's units, and
            # Finnhub answers first for US listings. Unconverted, AAPL's
            # 4.9M "market cap" failed the 250M floor and dropped out of the
            # Discover universe, and every US name maxed the ROE and growth
            # terms (137 read as 13,700%) (prod, 2026-09-26).
            # Current-price ratios on trailing-twelve-month figures, like
            # yfinance's trailingPE/priceToBook. The *Annual fields divide by
            # the last fiscal year's EPS (and pbAnnual even uses that year's
            # price): after a year of fast growth MU read P/E 143 and P/B 2.5
            # against a TTM P/E of 24 and a current P/B of 12 (prod,
            # 2026-09-28), and the dossier called it expensive on that basis.
            data = {
                "pe_ratio": _positive(
                    _first(metric, "peTTM", "peExclExtraTTM", "peBasicExclExtraTTM", "peNormalizedAnnual")
                ),
                "pb_ratio": _positive(_first(metric, "pb", "pbQuarterly", "pbAnnual")),
                "roe": _scaled(metric.get("roeTTM"), 0.01),
                "roic": _scaled(metric.get("roiTTM"), 0.01),
                "profit_margin": _scaled(metric.get("netProfitMarginTTM"), 0.01),
                "gross_margin": _scaled(metric.get("grossMarginTTM"), 0.01),
                # yfinance's debtToEquity is in percent (78.4 = 0.784x).
                "debt_equity": _scaled(
                    _first(metric, "totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual"), 100.0,
                ),
                "revenue_growth": _scaled(metric.get("revenueGrowthTTMYoy"), 0.01),
                "earnings_growth": _scaled(metric.get("epsGrowthTTMYoy"), 0.01),
                "dividend_yield": _scaled(metric.get("currentDividendYieldTTM"), 0.01),
                "beta": _float(metric.get("beta")),
                "market_cap": _scaled(profile.get("marketCapitalization"), 1e6),
                "volume": _scaled(metric.get("10DayAverageTradingVolume"), 1e6),
                "average_volume": _scaled(metric.get("3MonthAverageTradingVolume"), 1e6),
                "currency": profile.get("currency"),
                "country": profile.get("country"),
                "industry": profile.get("finnhubIndustry"),
                "name": profile.get("name"),
            }
            return provider_result(
                self.name,
                ok=bool(metric or profile),
                data=data,
                as_of=datetime.now(UTC),
                missing_fields=[key for key, value in data.items() if value in (None, "")],
                confidence=0.7,
            )
        except Exception as exc:
            return self.unavailable("fundamentals", str(exc))

    def get_news(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("news", "Finnhub API key is not configured.")
        if symbol and not is_us_listing(symbol):
            return self.unavailable("news", f"Finnhub covers US listings only; skipping {symbol}.")
        try:
            if symbol:
                # Finnhub does not understand yfinance-style exchange suffixes
                normalized = strip_exchange_suffix(symbol)
                to_day = datetime.now(UTC).date()
                from_day = to_day - timedelta(days=14)
                rows = self._get(
                    "/company-news",
                    {"symbol": normalized, "from": from_day.isoformat(), "to": to_day.isoformat()},
                )
            else:
                rows = self._get("/news", {"category": "general"})
            data = [
                {
                    "title": item.get("headline"),
                    "summary": item.get("summary"),
                    "url": item.get("url"),
                    "source": item.get("source"),
                    "published_at": item.get("datetime"),
                }
                for item in (rows or [])[:limit]
            ]
            return provider_result(self.name, ok=bool(data), data=data, as_of=datetime.now(UTC), confidence=0.65)
        except Exception as exc:
            return self.unavailable("news", str(exc))

    def get_analyst_estimates(self, symbol: str) -> dict[str, Any]:
        """Fetch price-target consensus plus a recommendation label.

        Finnhub splits this across two endpoints: ``/stock/price-target``
        (targetHigh/targetLow/targetMean/targetMedian — no current price, no
        recommendation) and ``/stock/recommendation`` (a list of per-period
        buy/hold/sell counts — no price targets). Neither endpoint alone
        matches what callers need; this combines both plus a quote lookup for
        current_price, normalized to the same snake_case shape the OpenBB
        provider returns from its consensus endpoint.

        ``/stock/price-target`` is a premium-only endpoint — verified live
        against the prod key, which is free-tier and gets HTTP 403 on it —
        so that fetch is isolated in its own try/except: a plan restriction
        there must not block the (free-tier) recommendation trend and quote,
        which still let this provider contribute a coarser buy/hold/sell
        signal via signal_breakdown's recommendation fallback.
        """
        if not self.api_key:
            return self.unavailable("analyst estimates", "Finnhub API key is not configured.")
        if not is_us_listing(symbol):
            return self.unavailable(
                "analyst estimates", f"Finnhub covers US listings only; skipping {symbol}."
            )
        try:
            normalized = strip_exchange_suffix(symbol)
            target_high = target_low = target_mean = target_median = None
            number_analysts = None
            try:
                targets = self._get("/stock/price-target", {"symbol": normalized})
                target_high = _float(targets.get("targetHigh"))
                target_low = _float(targets.get("targetLow"))
                target_mean = _float(targets.get("targetMean"))
                target_median = _float(targets.get("targetMedian"))
                number_analysts = targets.get("numberAnalysts")
            except Exception:
                pass  # price-target requires a paid Finnhub plan on some keys

            recommendation = None
            try:
                trend = self._get("/stock/recommendation", {"symbol": normalized})
                if isinstance(trend, list) and trend:
                    latest = trend[0]
                    buy = int(latest.get("buy") or 0) + int(latest.get("strongBuy") or 0)
                    sell = int(latest.get("sell") or 0) + int(latest.get("strongSell") or 0)
                    hold = int(latest.get("hold") or 0)
                    if buy > sell and buy > hold:
                        recommendation = "buy"
                    elif sell > buy and sell > hold:
                        recommendation = "sell"
                    elif buy or sell or hold:
                        recommendation = "hold"
            except Exception:
                pass  # recommendation trend is a nice-to-have, not required

            current_price = None
            try:
                quote_result = self.get_quote(symbol)
                if quote_result.get("ok") and quote_result.get("data"):
                    current_price = float(quote_result["data"]["close"])
            except Exception:
                pass  # current_price is a nice-to-have here too — callers can source it elsewhere

            data = {
                "symbol": symbol.upper(),
                "target_high": target_high,
                "target_low": target_low,
                "target_mean": target_mean,
                "target_median": target_median,
                "current_price": current_price,
                "recommendation": recommendation,
                "number_analysts": number_analysts,
            }
            ok = (
                target_high is not None
                or target_mean is not None
                or target_median is not None
                or recommendation is not None
            )
            return provider_result(
                self.name,
                ok=ok,
                data=data,
                as_of=datetime.now(UTC),
                missing_fields=[key for key, value in data.items() if value is None],
                confidence=0.7 if ok else 0.0,
            )
        except Exception as exc:
            return self.unavailable("analyst estimates", str(exc))

    def get_earnings_calendar(self, symbol: str) -> dict[str, Any]:
        """Next earnings report for a US listing from ``/calendar/earnings``.

        The free tier answers suffixed (non-US) symbols with HTTP 403, so
        those are declined without a request.
        """
        if not self.api_key:
            return self.unavailable("earnings calendar", "Finnhub API key is not configured.")
        if not is_us_listing(symbol):
            return self.unavailable("earnings calendar", "Finnhub's free earnings calendar covers US listings only.")
        try:
            today = datetime.now(UTC).date()
            payload = self._get(
                "/calendar/earnings",
                {
                    "symbol": strip_exchange_suffix(symbol),
                    "from": today.isoformat(),
                    "to": (today + timedelta(days=120)).isoformat(),
                },
            )
            rows = [
                r for r in (payload or {}).get("earningsCalendar") or []
                if isinstance(r, dict) and isinstance(r.get("date"), str) and r["date"] >= today.isoformat()
            ]
            if not rows:
                return self.unavailable("earnings calendar", f"Finnhub lists no upcoming report for {symbol}.")
            nxt = min(rows, key=lambda r: r["date"])
            hour = nxt.get("hour") if nxt.get("hour") in ("bmo", "amc", "dmh") else None
            data = {"symbol": symbol.upper(), "next_earnings_date": nxt["date"], "earnings_date_to": None, "hour": hour}
            return provider_result(self.name, ok=True, data=data, as_of=datetime.now(UTC), confidence=0.8)
        except Exception as exc:
            return self.unavailable("earnings calendar", str(exc))

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        with httpx.Client(timeout=12) as client:
            response = client.get(f"{self.base_url}{path}", params={**params, "token": self.api_key})
            if response.status_code >= 400:
                # Never re-raise httpx's default error: its message embeds the
                # full request URL including the token, which callers log.
                raise ValueError(
                    f"Finnhub HTTP {response.status_code} for {path} (symbol={params.get('symbol', '-')})"
                )
            return response.json()


def _scaled(value: Any, factor: float) -> float | None:
    """``_float(value) * factor``, or None when the value is missing."""
    parsed = _float(value)
    return None if parsed is None else parsed * factor


def _first(metric: dict[str, Any], *keys: str) -> float | None:
    """The first of *keys* that holds a number."""
    for key in keys:
        parsed = _float(metric.get(key))
        if parsed is not None:
            return parsed
    return None


def _positive(value: float | None) -> float | None:
    """None for a non-positive valuation ratio, as yfinance reports it.

    A negative P/E (a loss) is not a cheap stock; the fundamentals score
    would read it as one.
    """
    return value if value is not None and value > 0 else None


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
