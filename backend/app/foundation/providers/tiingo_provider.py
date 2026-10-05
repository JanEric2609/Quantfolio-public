from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.utils import split_exchange_suffix

logger = logging.getLogger(__name__)

# yfinance suffixes Tiingo natively covers besides US tickers.
_CN_SUFFIXES = {"SS", "SZ"}  # Shanghai / Shenzhen


def _unsupported_listing(symbol: str) -> bool:
    """True when ``symbol`` targets an exchange outside Tiingo's US/China coverage.

    Tiingo only serves US and Chinese listings (audit §15.1 G4); a suffixed EU
    symbol 404s and feeds retry loops. Known exchange suffixes (.L, .DE, …) and
    unknown multi-char suffixes (.XYZ) are refused; single-letter unknown
    suffixes are US share classes (BRK.B) and keep flowing.
    """
    if "." not in symbol:
        return False
    suffix = symbol.rsplit(".", 1)[1].upper()
    if suffix in _CN_SUFFIXES:
        return False
    _, mic = split_exchange_suffix(symbol)
    if mic is not None:
        return True
    return len(suffix) != 1


class TiingoProvider(MarketDataProvider):
    """Global market data provider backed by Tiingo.

    Tiingo provides clean, adjusted OHLCV data with 50+ years of history
    and includes both US and international symbols using native symbology.

    Auth: ``Authorization: Token <api_key>`` header.
    """

    name = "tiingo"
    capabilities = {"get_quote", "get_history"}

    def __init__(self, api_key: str | None, *, enabled: bool = True):
        super().__init__(enabled=enabled and bool(api_key))
        self.api_key = api_key
        self.base_url = "https://api.tiingo.com"

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": bool(self.api_key),
            "message": "Tiingo API key configured." if self.api_key else "Tiingo API key is not configured.",
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("quote", "Tiingo API key is not configured.")
        try:
            data = self._get("/iex/", {"tickers": symbol})
            records = data if isinstance(data, list) else []
            if not records:
                return self.unavailable("quote", f"Tiingo IEX returned no data for {symbol}.")
            latest = records[0]
            last = _float(latest.get("tngoLast"))
            bid = _float(latest.get("bidPrice"))
            ask = _float(latest.get("askPrice"))
            close = last
            if close is None and bid is not None and ask is not None:
                close = (bid + ask) / 2
            if close is None or close <= 0:
                return self.unavailable("quote", f"Tiingo returned no usable price for {symbol}.")
            as_of = _parse_ts(latest.get("timestamp")) or datetime.now(UTC)
            return provider_result(
                self.name,
                ok=True,
                data={
                    "symbol": symbol.upper(),
                    "date": as_of.date(),
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": Decimal(str(close)),
                    "volume": None,
                    "ask": Decimal(str(ask)) if ask else None,
                    "bid": Decimal(str(bid)) if bid else None,
                    "source": self.name,
                },
                as_of=as_of,
                confidence=0.7,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tiingo get_quote failed for %s: %s", symbol, exc)
            return self.unavailable("quote", str(exc))

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("history", "Tiingo API key is not configured.")
        if _unsupported_listing(symbol):
            return provider_result(
                self.name,
                ok=False,
                warnings=[f"tiingo: unsupported listing '{symbol}'"],
            )
        try:
            params: dict[str, Any] = {}
            if start:
                params["startDate"] = start
            if end:
                params["endDate"] = end
            if days and not start:
                from datetime import timedelta

                end_dt = datetime.fromisoformat(end).replace(tzinfo=UTC) if end else datetime.now(UTC)
                params["startDate"] = (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")
            data = self._get(f"/tiingo/daily/{symbol}/prices", params)
            records = data if isinstance(data, list) else []
            if not records:
                return self.unavailable("history", f"Tiingo returned no prices for {symbol}.")
            rows: list[dict[str, Any]] = []
            for r in records:
                close = _float(r.get("adjClose")) or _float(r.get("close"))
                if close is None:
                    continue
                rows.append({
                    "date": _parse_date(r.get("date")),
                    "open": Decimal(str(_float(r.get("adjOpen")) or _float(r.get("open")) or close)),
                    "high": Decimal(str(_float(r.get("adjHigh")) or _float(r.get("high")) or close)),
                    "low": Decimal(str(_float(r.get("adjLow")) or _float(r.get("low")) or close)),
                    "close": Decimal(str(close)),
                    "volume": _float(r.get("adjVolume")) or _float(r.get("volume")),
                })
            return provider_result(self.name, ok=bool(rows), data=rows, as_of=datetime.now(UTC), confidence=0.75)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tiingo get_history failed for %s: %s", symbol, exc)
            return self.unavailable("history", str(exc))

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Token {self.api_key}"}
        with httpx.Client(timeout=15) as client:
            response = client.get(f"{self.base_url}{path}", headers=headers, params=params)
            if response.status_code >= 400:
                # Never re-raise httpx's default error: even though the API key
                # travels via the Authorization header (not the URL) here, keep
                # the sanitized-error shape used across providers so this stays
                # safe if the auth mechanism ever changes.
                raise ValueError(f"Tiingo HTTP {response.status_code} for {path}")
            return response.json()


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_date(value: str | None) -> str | None:
    """Pass through ISO date strings, or return None."""
    if not value:
        return None
    return value[:10] if len(value) >= 10 else value
