"""JustETF UCITS ETF data provider (via justetf-scraping).

Wraps the justetf-scraping library (GitHub: druzsan/justetf-scraping, MIT).
Provides daily close prices and ETF profile data for UCITS ETFs by ISIN.

Limitations vs full MarketDataProvider:
- Daily close prices only (no OHLCV). Open/high/low/volume set to close/0.
- ISIN-based lookup only (no tickers). Symbols must be valid UCITS ETF ISINs.
- Web-scraping library — fragile to justETF.com structural changes.
- No API key required (free, anonymous access).
- GitHub-only install: pip install git+https://github.com/druzsan/justetf-scraping.git
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, timedelta
from typing import Any, Callable, TypeVar

import pandas as pd

from app.foundation.providers.base import MarketDataProvider, provider_result

_T = TypeVar("_T")
_JUSTETF_REQUEST_TIMEOUT_S = float(os.getenv("JUSTETF_REQUEST_TIMEOUT_S", "30"))


def _call_with_timeout(fn: Callable[[], _T], timeout_s: float, unavailable_msg: str) -> _T | dict[str, Any]:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn)
        return future.result(timeout=timeout_s)
    except FutureTimeoutError:
        return provider_result("justetf", ok=False, error=unavailable_msg)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


class JustETFProvider(MarketDataProvider):
    """UCITS ETF data from justETF.com via justetf-scraping library."""

    name = "justetf"
    capabilities = {"get_quote", "get_history"}

    def status(self) -> dict[str, Any]:
        try:
            import justetf_scraping  # noqa: F401

            return {
                "provider": self.name,
                "enabled": self.enabled,
                "available": True,
                "message": "justetf-scraping import ok. Covers ~3400 UCITS ETFs.",
            }
        except ImportError:
            return {
                "provider": self.name,
                "enabled": self.enabled,
                "available": False,
                "message": "justetf-scraping not installed. pip install git+https://github.com/druzsan/justetf-scraping.git",
            }
        except Exception as exc:
            return {"provider": self.name, "enabled": self.enabled, "available": False, "message": str(exc)}

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get real-time Gettex quote for an ETF ISIN.

        WARNING: load_live_quote() may hang for ETFs without a Gettex feed (issue #43).
        Timeout protection via ThreadPoolExecutor (default 30s).
        """
        if not self.enabled:
            return provider_result(self.name, ok=False, error="JustETF provider is disabled")

        try:
            import justetf_scraping
        except ImportError:
            return provider_result(self.name, ok=False, error="justetf-scraping not installed")

        def _fetch():
            quote = justetf_scraping.load_live_quote(symbol)
            return {
                "symbol": symbol,
                "price": float(quote.last) if quote.last else None,
                "bid": float(quote.bid) if quote.bid else None,
                "ask": float(quote.ask) if quote.ask else None,
                "currency": quote.currency,
                "exchange": quote.exchange,
                "timestamp": quote.timestamp.isoformat() if quote.timestamp else None,
            }

        return _call_with_timeout(
            _fetch,
            timeout_s=_JUSTETF_REQUEST_TIMEOUT_S,
            unavailable_msg=f"justETF live quote timed out after {_JUSTETF_REQUEST_TIMEOUT_S}s for {symbol}",
        )

    def get_history(self, symbol: str, start: str | None = None, end: str | None = None, days: int | None = None) -> dict[str, Any]:
        """Get daily close prices for a UCITS ETF by ISIN.

        Returns daily close prices from inception. Days parameter limits the
        number of most recent trading days returned. Since justETF only provides
        daily close (no OHLCV), open/high/low are set to close and volume is 0.
        """
        if not self.enabled:
            return provider_result(self.name, ok=False, error="JustETF provider is disabled")

        try:
            import justetf_scraping
        except ImportError:
            return provider_result(self.name, ok=False, error="justetf-scraping not installed")

        if end:
            end_date = date.fromisoformat(end)
        else:
            from datetime import UTC, datetime
            end_date = datetime.now(UTC).date()

        default_days = days if days else 365
        start_cutoff = end_date - timedelta(days=default_days)

        def _fetch():
            df = justetf_scraping.load_chart(symbol, currency="EUR")

            if df is None or df.empty:
                return None

            # Filter to date range
            df: pd.DataFrame = df.loc[df.index <= end_date]  # type: ignore[valid-type]
            df = df.loc[df.index >= start_cutoff]

            if df.empty:
                return None

            closes: list[dict[str, Any]] = []
            for idx, row in df.iterrows():
                dt = idx if isinstance(idx, date) else idx.date()  # type: ignore[union-attr]
                close_val = float(row.get("quote", 0.0) or 0.0)
                closes.append(
                    {
                        "date": dt.isoformat(),
                        "open": close_val,
                        "high": close_val,
                        "low": close_val,
                        "close": close_val,
                        "volume": 0,
                    }
                )

            return closes

        result = _call_with_timeout(
            _fetch,
            timeout_s=_JUSTETF_REQUEST_TIMEOUT_S,
            unavailable_msg=f"justETF chart load timed out after {_JUSTETF_REQUEST_TIMEOUT_S}s for {symbol}",
        )

        if isinstance(result, dict):
            return result  # Error result from timeout

        if result is None:
            return provider_result(self.name, ok=False, error=f"No data from justETF for {symbol}")

        return provider_result(
            self.name,
            ok=True,
            data=result,
        )
