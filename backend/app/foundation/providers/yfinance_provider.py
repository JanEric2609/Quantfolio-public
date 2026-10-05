from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, TypeVar, cast

from app.foundation.providers.base import MarketDataProvider, provider_result

_T = TypeVar("_T")
_YF_REQUEST_TIMEOUT_S = float(os.getenv("YFINANCE_REQUEST_TIMEOUT_S") or "30")


def _call_with_timeout(fn: Callable[[], _T], timeout_s: float, unavailable_msg: str) -> _T | dict[str, Any]:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn)
        return future.result(timeout=timeout_s)
    except FutureTimeoutError:
        return provider_result("yfinance", ok=False, error=unavailable_msg)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"
    capabilities = {
        "get_quote", "get_history", "get_fundamentals", "get_news", "get_fx_rate",
        "get_analyst_estimates", "get_earnings_calendar", "get_dividends",
    }

    def status(self) -> dict[str, Any]:
        try:
            import yfinance  # noqa: F401

            return {"provider": self.name, "enabled": self.enabled, "available": True, "message": "yfinance import is available."}
        except Exception as exc:
            return {"provider": self.name, "enabled": self.enabled, "available": False, "message": str(exc)}

    def get_quote(self, symbol: str) -> dict[str, Any]:
        try:
            import yfinance as yf

            def _fetch():
                ticker = yf.Ticker(symbol)
                frame = ticker.history(period="5d", auto_adjust=True)
                if frame.empty:
                    return self.unavailable("quote", "yfinance returned no quote rows.")
                info = ticker.info or {}
                currency = info.get("currency", "EUR")
                row = frame.iloc[-1]
                idx = frame.index[-1]
                close = row.get("Close")
                return provider_result(
                    self.name,
                    ok=True,
                    data={
                        "symbol": symbol.upper(),
                        "date": idx.date(),  # type: ignore[union-attr]
                        "open": Decimal(str(row.get("Open", close))),
                        "high": Decimal(str(row.get("High", close))),
                        "low": Decimal(str(row.get("Low", close))),
                        "close": Decimal(str(close)),
                        "volume": Decimal(str(row.get("Volume", 0))),
                        "currency": currency,
                        "source": self.name,
                    },
                    as_of=_as_utc(idx),
                    confidence=0.78,
                )

            return _call_with_timeout(
                _fetch,
                _YF_REQUEST_TIMEOUT_S,
                f"yfinance request timed out after {_YF_REQUEST_TIMEOUT_S}s",
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
        try:
            import yfinance as yf

            def _fetch():
                ticker = yf.Ticker(symbol)
                frame = ticker.history(
                    start=start,
                    end=end,
                    period=None if start else f"{max(5, days or 365)}d",
                    auto_adjust=True,
                )
                # The chart response that carries the bars also names their
                # quote unit ("GBp" for SHEL.L, "USD" for IWDA.L), at no extra
                # request. Without it every stored bar fell back to a guessed
                # label (data_backbone.listing_currency).
                meta = getattr(ticker, "history_metadata", None) or {}
                currency = meta.get("currency") if isinstance(meta, dict) else None
                return frame, currency

            frame_or_err = _call_with_timeout(
                _fetch,
                _YF_REQUEST_TIMEOUT_S,
                f"yfinance request timed out after {_YF_REQUEST_TIMEOUT_S}s",
            )
            if isinstance(frame_or_err, dict):
                return frame_or_err

            frame, currency = frame_or_err
            if frame.empty:
                return self.unavailable("history", "yfinance returned no history rows.")
            rows = []
            for idx, row in frame.iterrows():
                close = row.get("Close")
                rows.append(
                    {
                        "date": idx.date(),  # type: ignore[union-attr]
                        "open": Decimal(str(row.get("Open", close))),
                        "high": Decimal(str(row.get("High", close))),
                        "low": Decimal(str(row.get("Low", close))),
                        "close": Decimal(str(close)),
                        "volume": Decimal(str(row.get("Volume", 0))),
                        "source": self.name,
                        **({"currency": currency} if currency else {}),
                    }
                )
            warnings = []
            if len(rows) < 252:
                warnings.append("Less than one year of history is available.")
            return provider_result(
                self.name,
                ok=True,
                data=rows,
                as_of=_as_utc(frame.index[-1]),
                confidence=0.76 if len(rows) >= 252 else 0.55,
                warnings=warnings,
            )
        except Exception as exc:
            return self.unavailable("history", str(exc))

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        try:
            import yfinance as yf

            info = yf.Ticker(symbol).info or {}
            data = {
                "pe_ratio": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "pb_ratio": info.get("priceToBook"),
                "ev_ebitda": info.get("enterpriseToEbitda"),
                "fcf_yield": _fcf_yield(info),
                "roe": info.get("returnOnEquity"),
                "roic": info.get("returnOnCapital"),
                "profit_margin": info.get("profitMargins"),
                "gross_margin": info.get("grossMargins"),
                "debt_equity": info.get("debtToEquity"),
                "revenue_growth": info.get("revenueGrowth"),
                "earnings_growth": info.get("earningsGrowth"),
                "eps": info.get("trailingEps"),
                "market_cap": info.get("marketCap"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "currency": info.get("currency"),
                "beta": info.get("beta"),
                "volume": info.get("volume"),
                "average_volume": info.get("averageVolume"),
                "ter": info.get("annualReportExpenseRatio"),
                "aum": info.get("totalAssets"),
                "dividend_yield": _dividend_yield_fraction(info),
                "isin": info.get("isin"),
                "name": info.get("longName") or info.get("shortName"),
            }
            missing = [key for key, value in data.items() if value is None]
            return provider_result(
                self.name,
                ok=True,
                data=data,
                as_of=datetime.now(UTC),
                missing_fields=missing,
                confidence=0.72 if len(missing) < len(data) * 0.6 else 0.45,
            )
        except Exception as exc:
            return self.unavailable("fundamentals", str(exc))

    def get_analyst_estimates(self, symbol: str) -> dict[str, Any]:
        """Price-target consensus from Yahoo's ``.info`` payload.

        Same snake_case shape the OpenBB consensus endpoint and Finnhub return.
        OpenBB serves this data from yfinance anyway, but behind a self-imposed
        30/min guardrail that a ~300-candidate Discover run overruns; when the
        registry skipped a throttled OpenBB call, about half the candidates
        silently kept a neutral analyst placeholder (BBVA.MC audit, 2026-09-26).
        Asking Yahoo directly removes that bottleneck and, unlike Finnhub's free
        tier, covers non-US listings.

        ``recommendationMean`` is deliberately not mapped: it has been seen
        at 0.0 next to ``recommendationKey="strong_buy"`` (KER.PA), which is
        outside Yahoo's own 1-5 scale.
        """
        try:
            import yfinance as yf

            def _fetch():
                return yf.Ticker(symbol).info or {}

            info_or_err = _call_with_timeout(
                _fetch,
                _YF_REQUEST_TIMEOUT_S,
                f"yfinance request timed out after {_YF_REQUEST_TIMEOUT_S}s",
            )
            if not isinstance(info_or_err, dict):
                return self.unavailable("analyst estimates", "yfinance returned no info payload.")
            if info_or_err.get("provider") == self.name and info_or_err.get("ok") is False:
                return info_or_err  # the timeout result from _call_with_timeout
            info = info_or_err
            recommendation = info.get("recommendationKey")
            if not isinstance(recommendation, str) or recommendation.lower() == "none":
                recommendation = None
            data = {
                "symbol": symbol.upper(),
                "target_high": _positive_float(info.get("targetHighPrice")),
                "target_low": _positive_float(info.get("targetLowPrice")),
                "target_mean": _positive_float(info.get("targetMeanPrice")),
                "target_median": _positive_float(info.get("targetMedianPrice")),
                "current_price": _positive_float(
                    info.get("currentPrice") or info.get("regularMarketPrice")
                ),
                "recommendation": recommendation,
                "number_analysts": info.get("numberOfAnalystOpinions"),
                "currency": info.get("currency"),
                # Same payload, so passed along: US listings resolve
                # fundamentals via Finnhub, which carries no sector, and the
                # Discover shortlist's sector cap needs one.
                "sector": info.get("sector"),
                "industry": info.get("industry"),
            }
            ok = (
                data["target_mean"] is not None
                or data["target_median"] is not None
                or recommendation is not None
            )
            return provider_result(
                self.name,
                ok=ok,
                data=data,
                as_of=datetime.now(UTC),
                missing_fields=[key for key, value in data.items() if value is None],
                confidence=0.7 if ok else 0.0,
                warnings=[] if ok else [f"yfinance has no analyst coverage for {symbol}."],
            )
        except Exception as exc:
            return self.unavailable("analyst estimates", str(exc))

    def get_dividends(self, symbol: str, start: str) -> dict[str, Any]:
        """Cash distributions per share, in the listing's currency, by ex-date (Yahoo ``.dividends``)."""
        try:
            import yfinance as yf

            def _fetch():
                return yf.Ticker(symbol).dividends

            series = _call_with_timeout(
                _fetch,
                _YF_REQUEST_TIMEOUT_S,
                f"yfinance request timed out after {_YF_REQUEST_TIMEOUT_S}s",
            )
            if isinstance(series, dict):
                return series
            rows = [
                {"ex_date": day.isoformat(), "amount": float(value)}
                for idx, value in series.items()
                if (day := cast(datetime, idx).date()).isoformat() >= start and float(value) > 0
            ]
            return provider_result(self.name, ok=True, data=rows, as_of=datetime.now(UTC), confidence=0.8)
        except Exception as exc:
            return self.unavailable("dividends", str(exc))

    def get_earnings_calendar(self, symbol: str) -> dict[str, Any]:
        """Next scheduled earnings report from Yahoo's ``.calendar``.

        Covers European listings, which Finnhub's free earnings calendar
        answers with HTTP 403. Yahoo gives two dates when the company has not
        confirmed the day yet; both are passed on.
        """
        try:
            import yfinance as yf

            def _fetch():
                return yf.Ticker(symbol).calendar or {}

            cal = _call_with_timeout(
                _fetch,
                _YF_REQUEST_TIMEOUT_S,
                f"yfinance request timed out after {_YF_REQUEST_TIMEOUT_S}s",
            )
            if isinstance(cal, dict) and cal.get("provider") == self.name and cal.get("ok") is False:
                return cal
            raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
            dates = sorted(d for d in (raw or []) if isinstance(d, date))
            upcoming = [d for d in dates if d >= datetime.now(UTC).date()]
            if not upcoming:
                return self.unavailable("earnings calendar", f"Yahoo lists no upcoming report for {symbol}.")
            data = {
                "symbol": symbol.upper(),
                "next_earnings_date": upcoming[0].isoformat(),
                # Yahoo's window end when the date is not confirmed yet.
                "earnings_date_to": upcoming[-1].isoformat() if len(upcoming) > 1 else None,
                "hour": None,
            }
            return provider_result(self.name, ok=True, data=data, as_of=datetime.now(UTC), confidence=0.7)
        except Exception as exc:
            return self.unavailable("earnings calendar", str(exc))

    def get_news(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        try:
            import yfinance as yf

            if not symbol:
                return self.unavailable("news", "yfinance news requires a symbol.")
            news = yf.Ticker(symbol).news or []
            rows = [
                {
                    "title": item.get("title"),
                    "url": item.get("link") or item.get("url"),
                    "publisher": item.get("publisher"),
                    "published_at": item.get("providerPublishTime"),
                }
                for item in news[:limit]
            ]
            warnings = []
            if not rows:
                warnings.append(f"yfinance returned no news for {symbol}.")
            return provider_result(self.name, ok=bool(rows), data=rows, as_of=datetime.now(UTC), confidence=0.5, warnings=warnings)
        except Exception as exc:
            return self.unavailable("news", str(exc))

    def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        pair = f"{base.upper()}{quote.upper()}=X"
        result = self.get_quote(pair)
        if result["ok"] and result["data"]:
            result["data"] = {"base": base.upper(), "quote": quote.upper(), "rate": float(result["data"]["close"])}
        return result


def _as_utc(value: Any) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    return datetime.now(UTC) - timedelta(days=1)


def _positive_float(value: Any) -> float | None:
    """A strictly positive float, else None (Yahoo pads missing prices with 0)."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _fcf_yield(info: dict[str, Any]) -> float | None:
    free_cashflow = info.get("freeCashflow")
    market_cap = info.get("marketCap")
    if not free_cashflow or not market_cap:
        return None
    try:
        return float(free_cashflow) / float(market_cap)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dividend_yield_fraction(info: dict[str, Any]) -> float | None:
    """Dividend yield as an annual fraction (e.g. 0.037 for 3.7%).

    Since yfinance 0.2.54 (pinned 1.4.1) ``dividendYield`` is a percentage:
    AAPL reads 0.32 for a 0.32% yield (prod, 2026-09-26; Finnhub's
    ``currentDividendYieldTTM`` gave 0.3151). The previous rule divided only
    values above 0.5, so every yield under 0.5% was stored 100x too high.
    ``trailingAnnualDividendYield`` is still a fraction and serves as the
    fallback.
    """
    raw = info.get("dividendYield")
    scale = 100.0
    if raw is None:
        raw = info.get("trailingAnnualDividendYield")
        scale = 1.0
    if raw is None:
        return None
    try:
        value = float(raw) / scale
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None
