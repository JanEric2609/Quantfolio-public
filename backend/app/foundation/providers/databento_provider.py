from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.utils import is_us_listing, strip_exchange_suffix

logger = logging.getLogger(__name__)


class DatabentoProvider(MarketDataProvider):
    """US equities market data provider backed by Databento.

    Databento provides tick-level and aggregated data for US equities
    via a key-only REST API. Free tier: 250K messages/month.
    The provider uses the ``XNAS.ITCH`` dataset for OHLCV bars.
    """

    name = "databento"
    capabilities = {"get_quote", "get_history"}

    def __init__(self, api_key: str | None, *, enabled: bool = True):
        super().__init__(enabled=enabled and bool(api_key))
        self.api_key = api_key
        self.base_url = "https://hist.databento.com/v0"

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": bool(self.api_key),
            "message": "Databento API key configured." if self.api_key else "Databento API key is not configured.",
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("quote", "Databento API key is not configured.")
        if not is_us_listing(symbol):
            # XNAS.ITCH is Nasdaq-only; a stripped EU symbol can collide with
            # an unrelated US ticker and return the wrong price.
            return self.unavailable("quote", f"Databento covers US listings only; skipping {symbol}.", reason="not_applicable")
        try:
            normalized = strip_exchange_suffix(symbol)
            data = self._get(
                "/timeseries.get_range",
                {
                    "dataset": "XNAS.ITCH",
                    "symbols": normalized,
                    "schema": "ohlcv-1d",
                    "start": _days_ago_str(5),
                },
            )
            records = data if isinstance(data, list) else []
            if not records:
                return self.unavailable("quote", f"Databento returned no data for {normalized}.", reason="no_data")
            latest = records[-1]
            close = _float(latest.get("close"))
            high = _float(latest.get("high"))
            low = _float(latest.get("low"))
            open_ = _float(latest.get("open"))
            if close is None:
                return self.unavailable("quote", f"Databento returned no close price for {normalized}.", reason="no_data")
            as_of = _parse_ts(latest.get("ts_event")) or datetime.now(UTC)
            return provider_result(
                self.name,
                ok=True,
                data={
                    "symbol": symbol.upper(),
                    "date": as_of.date(),
                    "open": Decimal(str(open_ or close)),
                    "high": Decimal(str(high or close)),
                    "low": Decimal(str(low or close)),
                    "close": Decimal(str(close)),
                    "volume": _float(latest.get("volume")),
                    "source": self.name,
                },
                as_of=as_of,
                confidence=0.75,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Databento get_quote failed for %s: %s", symbol, exc)
            return self.unavailable("quote", str(exc))

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("history", "Databento API key is not configured.")
        if not is_us_listing(symbol):
            return self.unavailable("history", f"Databento covers US listings only; skipping {symbol}.", reason="not_applicable")
        try:
            normalized = strip_exchange_suffix(symbol)
            params: dict[str, Any] = {"dataset": "XNAS.ITCH", "symbols": normalized, "schema": "ohlcv-1d"}
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            if days and not start:
                params["start"] = _days_ago_str(days, end=end)
            data = self._get("/timeseries.get_range", params=params)
            records = data if isinstance(data, list) else []
            if not records:
                return self.unavailable("history", f"Databento returned no bars for {normalized}.", reason="no_data")
            rows: list[dict[str, Any]] = []
            for r in records:
                close = _float(r.get("close"))
                if close is None:
                    continue
                row: dict[str, Any] = {
                    "date": _parse_ts(r.get("ts_event")),
                    "open": Decimal(str(_float(r.get("open")) or close)),
                    "high": Decimal(str(_float(r.get("high")) or close)),
                    "low": Decimal(str(_float(r.get("low")) or close)),
                    "close": Decimal(str(close)),
                    "volume": _float(r.get("volume")),
                }
                rows.append(row)
            return provider_result(self.name, ok=bool(rows), data=rows, as_of=datetime.now(UTC), confidence=0.75)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Databento get_history failed for %s: %s", symbol, exc)
            return self.unavailable("history", str(exc))

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        assert self.api_key is not None  # guarded by callers
        headers = {"Accept": "application/json"}
        params["encoding"] = "json"  # Databento REST returns DBN binary by default
        with httpx.Client(timeout=15, auth=(self.api_key, "")) as client:
            response = client.post(f"{self.base_url}{path}", headers=headers, json=params)
            if response.status_code >= 400:
                # Never re-raise httpx's default error: even though the API key
                # travels via HTTP Basic auth (not the URL) here, keep the
                # sanitized-error shape used across providers so this stays
                # safe if the auth mechanism ever changes.
                raise ValueError(
                    f"Databento HTTP {response.status_code} for {path} (symbols={params.get('symbols', '-')})"
                )
            return response.json()


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _days_ago_str(days: int, end: str | None = None) -> str:
    from datetime import timedelta
    if end:
        end_dt = datetime.fromisoformat(end)
        return (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


def _parse_ts(ns: Any) -> datetime | None:
    """Parse a Databento nanosecond timestamp (integer)."""
    if ns is None:
        return None
    try:
        return datetime.fromtimestamp(float(ns) / 1_000_000_000, UTC)
    except (TypeError, ValueError):
        return None
