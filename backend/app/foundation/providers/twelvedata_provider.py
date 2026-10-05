from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.utils import split_exchange_suffix

logger = logging.getLogger(__name__)


class TwelveDataProvider(MarketDataProvider):
    """Global market data provider backed by Twelve Data.

    Free tier: 800 API calls/day, 8 calls/minute, 130+ technical indicators,
    delayed data (4h). Covers 100+ exchanges worldwide.

    Twelve Data does not understand yfinance exchange suffixes (EOAN.DE 404s);
    non-US listings are addressed as base symbol + `mic_code` (EOAN + XETR).
    """

    name = "twelvedata"
    capabilities = {"get_quote", "get_history"}

    def __init__(self, api_key: str | None, *, enabled: bool = True):
        super().__init__(enabled=enabled and bool(api_key))
        self.api_key = api_key
        self.base_url = "https://api.twelvedata.com"

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": self.enabled,
            "available": bool(self.api_key),
            "message": "Twelve Data API key configured." if self.api_key else "Twelve Data API key is not configured.",
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("quote", "Twelve Data API key is not configured.")
        try:
            data = self._get("/quote", _symbol_params(symbol))
            if not isinstance(data, dict):
                return self.unavailable("quote", f"Twelve Data returned unexpected response for {symbol}.")
            close = _float(data.get("close"))
            if close is None or close <= 0:
                return self.unavailable("quote", f"Twelve Data returned no close price for {symbol}.")
            return provider_result(
                self.name,
                ok=True,
                data={
                    "symbol": symbol.upper(),
                    "date": datetime.now(UTC).date(),
                    "open": Decimal(str(_float(data.get("open")) or close)),
                    "high": Decimal(str(_float(data.get("high")) or close)),
                    "low": Decimal(str(_float(data.get("low")) or close)),
                    "close": Decimal(str(close)),
                    "volume": _float(data.get("volume")),
                    "source": self.name,
                },
                as_of=datetime.now(UTC),
                confidence=0.75,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("TwelveData get_quote failed for %s: %s", symbol, exc)
            return self.unavailable("quote", str(exc))

    def get_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            return self.unavailable("history", "Twelve Data API key is not configured.")
        if _unsupported_listing(symbol):
            return provider_result(
                self.name,
                ok=False,
                warnings=[f"twelvedata: unsupported listing '{symbol}'"],
            )
        try:
            params: dict[str, Any] = {**_symbol_params(symbol), "interval": "1day", "outputsize": 5000}
            if start:
                params["start_date"] = start
            elif days:
                from datetime import timedelta

                end_dt = datetime.fromisoformat(end).replace(tzinfo=UTC) if end else datetime.now(UTC)
                params["start_date"] = (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")
            if end:
                params["end_date"] = end
            data = self._get("/time_series", params=params)
            values = data.get("values") if isinstance(data, dict) else None
            if not isinstance(values, list):
                return self.unavailable("history", f"Twelve Data returned no values for {symbol}.")
            rows: list[dict[str, Any]] = []
            for v in values:
                close = _float(v.get("close"))
                if close is None:
                    continue
                ts = v.get("datetime")
                parsed = _parse_iso(ts) if ts else None
                rows.append({
                    "date": parsed.date() if parsed else ts,
                    "open": Decimal(str(_float(v.get("open")) or close)),
                    "high": Decimal(str(_float(v.get("high")) or close)),
                    "low": Decimal(str(_float(v.get("low")) or close)),
                    "close": Decimal(str(close)),
                    "volume": _float(v.get("volume")),
                })
            return provider_result(self.name, ok=bool(rows), data=rows, as_of=datetime.now(UTC), confidence=0.75)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TwelveData get_history failed for %s: %s", symbol, exc)
            return self.unavailable("history", str(exc))

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{self.base_url}{path}",
                params={**params, "apikey": self.api_key},
            )
            if response.status_code >= 400:
                # Never re-raise httpx's default error: its message embeds the
                # full request URL including the apikey, which callers log.
                raise ValueError(
                    f"Twelve Data HTTP {response.status_code} for {path} "
                    f"(symbol={params.get('symbol')}, mic_code={params.get('mic_code', '-')})"
                )
            body = response.json()
            # Twelve Data returns 200 with JSON error body on API-level errors
            if isinstance(body, dict) and body.get("status") == "error":
                msg = body.get("message", body.get("error", "unknown error"))
                raise ValueError(f"Twelve Data API error: {msg}")
            return body


def _symbol_params(symbol: str) -> dict[str, Any]:
    """Translate a yfinance-style symbol into Twelve Data request params."""
    base, mic = split_exchange_suffix(symbol)
    if mic is not None:
        return {"symbol": base, "mic_code": mic}
    return {"symbol": symbol}


def _unsupported_listing(symbol: str) -> bool:
    """True when ``symbol`` is a dot-suffixed listing Twelve Data cannot serve.

    Twelve Data addresses non-US listings as bare symbol + ``mic_code``, but its
    history endpoint 404s on many such listings (IWDA + XLON), producing eternal
    retry loops (audit §2.1). Known exchange suffixes (.L, .DE, .PA, …) and any
    unknown multi-char suffix (.XYZ — a bare-symbol fallback could silently
    resolve to the wrong instrument) are refused; single-letter unknown suffixes
    are US share classes (BRK.B) and keep flowing.
    """
    if "." not in symbol:
        return False
    _, mic = split_exchange_suffix(symbol)
    if mic is not None:
        return True
    suffix = symbol.rsplit(".", 1)[1].upper()
    return len(suffix) != 1


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
