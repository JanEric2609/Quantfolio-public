"""Massive.com (Polygon.io rebrand) market data provider."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result

logger = logging.getLogger(__name__)

POLYGON_BASE_URL = "https://api.polygon.io"
POLYGON_TIMEOUT = 15.0


class MassiveProvider(MarketDataProvider):
    """Market data provider backed by Polygon.io (Massive.com rebrand) REST API."""

    name = "massive"
    capabilities = {"get_quote", "get_history", "get_fundamentals"}

    def __init__(self, api_key: str | None, *, enabled: bool = True):
        self.api_key = api_key
        self.enabled = enabled and bool(api_key)
        self.base_url = POLYGON_BASE_URL

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Make GET request to Polygon API. Returns parsed JSON or None on error."""
        params = dict(params) if params else {}
        # Fallback: also send apiKey as query param for endpoints that prefer it
        params.setdefault("apiKey", self.api_key)
        try:
            with httpx.Client(base_url=self.base_url, timeout=POLYGON_TIMEOUT) as client:
                resp = client.get(path, headers=self._headers(), params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("Polygon API HTTP error %s: %s", e.response.status_code, e.response.text[:200])
            return None
        except httpx.RequestError as e:
            logger.warning("Polygon API request failed: %s", e)
            return None

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"provider": self.name, "enabled": False, "available": False, "message": "No API key configured"}
        try:
            # Quick connectivity check — just verify the API responds
            data = self._get("/v2/aggs/ticker/AAPL/prev")
            available = data is not None
            market_status = self._get("/v1/marketstatus/now")
            market_is_open = market_status is not None and market_status.get("market") == "open"
            return {
                "provider": self.name,
                "enabled": True,
                "available": available,
                "market_open": market_is_open,
                "message": "Available" if available else "API not reachable",
            }
        except Exception as e:
            return {"provider": self.name, "enabled": True, "available": False, "message": str(e)}

    def get_quote(self, symbol: str) -> dict[str, Any]:
        if not self.enabled:
            return self.unavailable("quote")
        data = self._get(f"/v2/aggs/ticker/{symbol.upper()}/prev")
        if not data or not data.get("results"):
            return provider_result(self.name, ok=False, error="No quote data for {}".format(symbol))
        r = data["results"][0]
        return provider_result(
            self.name, ok=True,
            data={
                "symbol": symbol.upper(),
                "date": _ms_timestamp_to_date(r.get("t")),
                "open": Decimal(str(r["o"])),
                "high": Decimal(str(r["h"])),
                "low": Decimal(str(r["l"])),
                "close": Decimal(str(r["c"])),
                "volume": int(r["v"]),
                "currency": "USD",
                "source": self.name,
            },
            confidence=0.85,
        )

    def get_history(self, symbol: str, start: datetime | None = None, end: datetime | None = None, days: int | None = None) -> dict[str, Any]:
        if not self.enabled:
            return self.unavailable("history")
        # Polygon uses YYYY-MM-DD format
        if end:
            to_date = end.date().isoformat()
        else:
            to_date = datetime.now(timezone.utc).date().isoformat()
        if start:
            from_date = start.date().isoformat()
        elif days:
            from_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        else:
            # Default to 30 days if no range specified
            from_date = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()

        path = f"/v2/aggs/ticker/{symbol.upper()}/range/1/day/{from_date}/{to_date}"
        data = self._get(path)
        if data is None or not data.get("results"):
            return provider_result(self.name, ok=False, error="No history data")

        history: list[dict[str, Any]] = []
        for r in data["results"]:
            try:
                ts_ms = r.get("t")
                date_str = _ms_timestamp_to_date(ts_ms) if ts_ms else None
                history.append({
                    "date": date_str,
                    "open": float(r["o"]),
                    "high": float(r["h"]),
                    "low": float(r["l"]),
                    "close": float(r["c"]),
                    "volume": int(r["v"]),
                })
            except (ValueError, TypeError, KeyError):
                logger.warning("Skipping invalid history entry for %s: %s", symbol, r)
        if not history:
            return provider_result(self.name, ok=False, error="No valid history data")
        return provider_result(
            self.name, ok=True, data=history,
        )

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        if not self.enabled:
            return self.unavailable("fundamentals")
        data = self._get(f"/v3/reference/tickers/{symbol.upper()}")
        if not data or data.get("status") == "NOT_FOUND":
            return provider_result(self.name, ok=False, error="Fundamentals not found for {}".format(symbol))
        results = data.get("results", {})
        if not results:
            return provider_result(self.name, ok=False, error="No fundamental data for {}".format(symbol))
        return provider_result(
            self.name, ok=True,
            data={
                "symbol": symbol.upper(),
                "name": results.get("name"),
                "description": results.get("description"),
                "industry": results.get("sic_description"),
                "market_cap": results.get("market_cap"),
                "employees": results.get("total_employees"),
                "shares_outstanding": results.get("weighted_shares_outstanding"),
                "currency": results.get("currency_name", "USD"),
                "source": self.name,
            },
            confidence=0.7,
            missing_fields=["pe_ratio", "pb_ratio", "roe", "dividend_yield"],
        )

    def get_fx_rate(self, base: str, quote: str) -> dict[str, Any]:
        if not self.enabled:
            return self.unavailable("fx_rate")
        return provider_result(
            self.name,
            ok=False,
            error="Endpoint not available for Massive provider",
        )


def _ms_timestamp_to_date(ts_ms: int | None) -> str | None:
    """Convert millisecond timestamp to ISO date string."""
    if ts_ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        return dt.date().isoformat()
    except (ValueError, TypeError, OSError):
        return None
