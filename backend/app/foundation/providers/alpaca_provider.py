from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.utils import is_us_listing, strip_exchange_suffix

logger = logging.getLogger(__name__)


class AlpacaProvider(MarketDataProvider):
    """US equities market data provider backed by Alpaca Markets data API.

    Requires both an API key and a secret key; enabled only when both are set.
    Alpaca covers US-listed equities only — exchange suffixes are stripped
    before issuing requests.
    """

    name = "alpaca"
    capabilities = {"get_quote", "get_history"}

    def __init__(self, api_key: str | None, api_secret: str | None, *, enabled: bool = True):
        super().__init__(enabled=enabled and bool(api_key) and bool(api_secret))
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://data.alpaca.markets/v2"

    def status(self) -> dict[str, Any]:
        configured = bool(self.api_key) and bool(self.api_secret)
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": configured,
            "message": "Alpaca API key and secret configured." if configured else "Alpaca API key/secret is not configured.",
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        if not self.enabled or not self.api_key or not self.api_secret:
            return self.unavailable("quote", "Alpaca API key/secret is not configured.")
        if not is_us_listing(symbol):
            # Stripping the suffix risks matching an unrelated US ticker
            # (SHEL.AS → SHEL) and returning the wrong price.
            return self.unavailable("quote", f"Alpaca covers US listings only; skipping {symbol}.", reason="not_applicable")
        try:
            normalized = strip_exchange_suffix(symbol)
            data = self._get(f"/stocks/{normalized}/quotes/latest")
            quote = data.get("quote") if isinstance(data, dict) else None
            if not isinstance(quote, dict):
                return self.unavailable("quote", f"Alpaca returned no quote for {normalized}.")
            ap = _float(quote.get("ap"))
            bp = _float(quote.get("bp"))
            if ap is None or bp is None or ap <= 0 or bp <= 0:
                return self.unavailable("quote", f"Alpaca returned no usable quote for {normalized}.")
            # Use the midpoint of bid/ask as the "close" price.
            close = (Decimal(str(ap)) + Decimal(str(bp))) / Decimal("2")
            as_of = _parse_iso(quote.get("t")) or datetime.now(UTC)
            return provider_result(
                self.name,
                ok=True,
                data={
                    "symbol": symbol.upper(),
                    "date": as_of.date(),
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": close,
                    "volume": None,
                    "ask": Decimal(str(ap)),
                    "bid": Decimal(str(bp)),
                    "source": self.name,
                },
                as_of=as_of,
                confidence=0.7,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alpaca get_quote failed for %s: %s", symbol, exc)
            return self.unavailable("quote", str(exc))

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        if not self.enabled or not self.api_key or not self.api_secret:
            return self.unavailable("history", "Alpaca API key/secret is not configured.")
        if not is_us_listing(symbol):
            return self.unavailable("history", f"Alpaca covers US listings only; skipping {symbol}.", reason="not_applicable")
        try:
            normalized = strip_exchange_suffix(symbol)
            params: dict[str, Any] = {"timeframe": "1Day"}
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            data = self._get(f"/stocks/{normalized}/bars", params=params)
            bars = data.get("bars") if isinstance(data, dict) else None
            if not isinstance(bars, list):
                return self.unavailable("history", f"Alpaca returned no bars for {normalized}.")
            rows: list[dict[str, Any]] = []
            for bar in bars:
                if not isinstance(bar, dict):
                    continue
                as_of = _parse_iso(bar.get("t"))
                rows.append(
                    {
                        "date": as_of.date() if as_of else None,
                        "open": _decimal(bar.get("o")),
                        "high": _decimal(bar.get("h")),
                        "low": _decimal(bar.get("l")),
                        "close": _decimal(bar.get("c")),
                        "volume": _decimal(bar.get("v")),
                        "source": self.name,
                    }
                )
            if days and len(rows) > days:
                rows = rows[-days:]
            if not rows:
                return self.unavailable("history", f"Alpaca returned empty bars for {normalized}.")
            return provider_result(
                self.name,
                ok=True,
                data=rows,
                as_of=datetime.now(UTC),
                confidence=0.75 if len(rows) >= 252 else 0.55,
                warnings=[] if len(rows) >= 252 else ["Less than one year of Alpaca history is available."],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alpaca get_history failed for %s: %s", symbol, exc)
            return self.unavailable("history", str(exc))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # `self.enabled` is only true when both api_key and api_secret are set,
        # so headers will always carry non-empty strings here.
        headers = cast(
            dict[str, str],
            {
                "APCA-API-KEY-ID": self.api_key or "",
                "APCA-API-SECRET-KEY": self.api_secret or "",
            },
        )
        with httpx.Client(timeout=15) as client:
            response = client.get(f"{self.base_url}{path}", headers=headers, params=params or {})
            if response.status_code >= 400:
                # Never re-raise httpx's default error: even though the API
                # key/secret travel via headers (not the URL) here, keep the
                # sanitized-error shape used across providers so this stays
                # safe if the auth mechanism ever changes.
                raise ValueError(f"Alpaca HTTP {response.status_code} for {path}")
            return response.json()


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        # Alpaca timestamps are ISO 8601 in UTC, e.g. "2024-05-01T13:30:00Z".
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
