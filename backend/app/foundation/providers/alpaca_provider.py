from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
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

    def latest_trade(self, symbol: str) -> dict[str, Any] | None:
        """The last trade on the free IEX feed: ``{"price": Decimal, "as_of": datetime}``, or ``None`` when there
        is none. A failed read raises (``ValueError``/``httpx.HTTPError``), like the other market reads."""
        if not self.enabled or not is_us_listing(symbol):
            return None
        data = self._get(f"/stocks/{strip_exchange_suffix(symbol)}/trades/latest", {"feed": "iex"})
        trade = data.get("trade") if isinstance(data, dict) else None
        price = _decimal(trade.get("p")) if isinstance(trade, dict) else None
        as_of = _parse_iso(trade.get("t")) if isinstance(trade, dict) else None
        if price is None or price <= 0 or as_of is None:
            return None
        return {"price": price, "as_of": as_of}

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
            # Alpaca's default `adjustment=raw` stores unadjusted closes; a
            # stored series must be total return (plan 2026-10-12, D1).
            params: dict[str, Any] = {"timeframe": "1Day", "adjustment": "all"}
            if not start and days:
                # Without `start` Alpaca answers from the start of the current
                # day (two bars). Trading days -> calendar days, plus holidays.
                start = (datetime.now(UTC).date() - timedelta(days=math.ceil(days * 1.6))).isoformat()
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            params["limit"] = 10000
            bars: list[Any] | None = None
            for _page in range(50):
                data = self._get(f"/stocks/{normalized}/bars", params=params)
                page = data.get("bars") if isinstance(data, dict) else None
                if not isinstance(page, list):
                    break
                bars = [*(bars or []), *page]
                token = data.get("next_page_token") if isinstance(data, dict) else None
                if not token:
                    break
                params["page_token"] = token
            if bars is None:
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
            rows, cut = cut_at_listing_gap(rows)
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
                warnings=[
                    *([] if len(rows) >= 252 else ["Less than one year of Alpaca history is available."]),
                    *([f"Alpaca history cut at a gap of {cut} days: an earlier company may have used this ticker."] if cut else []),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alpaca get_history failed for %s: %s", symbol, exc)
            return self.unavailable("history", str(exc))

    def get_closes_multi(
        self, symbols: list[str], start: str, end: str | None = None, *, feed: str = "sip",
    ) -> dict[str, dict[str, float]]:
        """``{symbol: {YYYY-MM-DD: adjusted close}}`` for many US symbols in few calls.

        One request covers up to ``_MULTI_BATCH`` symbols and pages through
        ``next_page_token``; the free plan allows 200 calls a minute, so the
        whole US universe over a couple of weeks is a handful of calls. Keys
        are the symbols as passed. Raises ``ValueError`` on an HTTP error, so
        a caller can retry with ``feed="iex"`` when SIP history is refused.
        """
        if not self.enabled:
            raise ValueError("Alpaca API key/secret is not configured.")
        by_plain = {strip_exchange_suffix(s): s for s in symbols if is_us_listing(s)}
        out: dict[str, dict[str, float]] = {}
        plain = sorted(by_plain)
        for i in range(0, len(plain), self._MULTI_BATCH):
            batch = plain[i : i + self._MULTI_BATCH]
            params: dict[str, Any] = {
                "symbols": ",".join(batch), "timeframe": "1Day", "adjustment": "all",
                "feed": feed, "start": start, "limit": 10000,
            }
            if end:
                params["end"] = end
            for _page in range(50):
                data = self._get("/stocks/bars", params=params)
                for sym, bars in (data.get("bars") or {}).items():
                    target = out.setdefault(str(by_plain.get(str(sym)) or sym), {})
                    for bar in bars or []:
                        as_of = _parse_iso(bar.get("t"))
                        close = _float(bar.get("c"))
                        if as_of is not None and close is not None and close > 0:
                            target[as_of.date().isoformat()] = close
                token = data.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        return out

    _MULTI_BATCH = 100
    news_url = "https://data.alpaca.markets/v1beta1/news"

    def get_intraday_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime, *, feed: str = "iex",
    ) -> list[dict[str, Any]]:
        """Intraday bars (``30Min`` and so on) of a US symbol, oldest first, on the free IEX feed.

        Raw prices: an intraday return needs no dividend adjustment. Each row is
        ``{"t": datetime (UTC), "o", "h", "l", "c", "v"}`` with ``Decimal`` prices.
        Raises ``ValueError`` on an HTTP error or when the provider is not configured.
        """
        if not self.enabled:
            raise ValueError("Alpaca API key/secret is not configured.")
        params: dict[str, Any] = {
            "timeframe": timeframe, "adjustment": "raw", "feed": feed, "limit": 10000,
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        rows: list[dict[str, Any]] = []
        for _page in range(20):
            data = self._get(f"/stocks/{strip_exchange_suffix(symbol)}/bars", params=params)
            for bar in data.get("bars") or []:
                stamp = _parse_iso(bar.get("t")) if isinstance(bar, dict) else None
                if stamp is None:
                    continue
                rows.append({"t": stamp, "o": _decimal(bar.get("o")), "h": _decimal(bar.get("h")),
                             "l": _decimal(bar.get("l")), "c": _decimal(bar.get("c")), "v": _decimal(bar.get("v"))})
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return sorted(rows, key=lambda r: r["t"])

    screener_url = "https://data.alpaca.markets/v1beta1/screener/stocks"

    def _screener(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """A market-data GET on the screener. A 403 means the plan has no screener: ``DataUnavailable``."""
        if not self.enabled:
            raise ValueError("Alpaca API key/secret is not configured.")
        try:
            return self._get(path, params=params, base=self.screener_url)
        except ValueError as exc:
            if "HTTP 403" in str(exc):
                raise DataUnavailable("screener_not_entitled") from exc
            raise

    def screener_movers(self, top: int = 50) -> list[dict[str, Any]]:
        """Today's top gainers: ``{"symbol", "percent_change", "price"}`` (floats), best first."""
        data = self._screener("/movers", {"top": max(1, min(top, 50))})
        out: list[dict[str, Any]] = []
        for row in data.get("gainers") or []:
            if isinstance(row, dict) and row.get("symbol"):
                pc, px = _float(row.get("percent_change")), _float(row.get("price"))
                if pc is not None and px is not None:
                    out.append({"symbol": str(row["symbol"]).upper(), "percent_change": pc, "price": px})
        return out

    def screener_most_actives(self, top: int = 100) -> list[dict[str, Any]]:
        """The most active symbols by volume: ``{"symbol", "volume"}``."""
        data = self._screener("/most-actives", {"by": "volume", "top": max(1, min(top, 100))})
        out: list[dict[str, Any]] = []
        for row in data.get("most_actives") or []:
            if isinstance(row, dict) and row.get("symbol"):
                out.append({"symbol": str(row["symbol"]).upper(), "volume": _float(row.get("volume")) or 0.0})
        return out

    options_url = "https://data.alpaca.markets/v1beta1/options/snapshots"

    def option_chain(self, underlying: str, expiration_gte: date, expiration_lte: date,
                     feed: str = "indicative", before_request: Callable[[], None] | None = None
                     ) -> list[dict[str, Any]]:
        """Option snapshots of one underlying (market data only): ``{"symbol", "type", "strike", "expiration",
        "iv", "delta"}``, at most three pages. Alpaca sends implied volatility and Greeks but no open interest.
        A 403 (no options entitlement) is ``DataUnavailable("options_not_entitled")``. *before_request* runs
        before every page's GET (a caller's rate limiter: each page is one request)."""
        if not self.enabled:
            raise ValueError("Alpaca API key/secret is not configured.")
        params: dict[str, Any] = {"feed": feed, "limit": 1000, "expiration_date_gte": expiration_gte.isoformat(),
                                  "expiration_date_lte": expiration_lte.isoformat()}
        out: list[dict[str, Any]] = []
        for _page in range(3):
            if before_request is not None:
                before_request()
            try:
                data = self._get(f"/{strip_exchange_suffix(underlying)}", params=params, base=self.options_url)
            except ValueError as exc:
                if "HTTP 403" in str(exc):
                    raise DataUnavailable("options_not_entitled") from exc
                raise
            for occ, snap in (data.get("snapshots") or {}).items():
                row = _parse_occ(str(occ), snap if isinstance(snap, dict) else {})
                if row is not None:
                    out.append(row)
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out

    def get_news(self, symbols: list[str], start: datetime, limit: int = 20) -> list[dict[str, Any]]:
        """Headlines for US symbols since *start*: ``{"id", "headline", "created_at", "symbols"}``.

        Polled over REST (no WebSocket in Phase 1). Raises ``ValueError`` on an HTTP error.
        """
        if not self.enabled:
            raise ValueError("Alpaca API key/secret is not configured.")
        plain = [strip_exchange_suffix(s) for s in symbols if is_us_listing(s)]
        if not plain:
            return []
        params = {
            "symbols": ",".join(sorted(set(plain))), "limit": max(1, min(limit, 50)), "sort": "desc",
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        data = self._get("", params=params, base=self.news_url)
        out: list[dict[str, Any]] = []
        for item in data.get("news") or []:
            if isinstance(item, dict) and item.get("headline"):
                out.append({"id": str(item.get("id")), "headline": str(item["headline"])[:300],
                            "created_at": str(item.get("created_at") or ""), "symbols": list(item.get("symbols") or [])})
        return out

    def _get(self, path: str, params: dict[str, Any] | None = None, *, base: str | None = None) -> dict[str, Any]:
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
            response = client.get(f"{base or self.base_url}{path}", headers=headers, params=params or {})
            if response.status_code >= 400:
                # Never re-raise httpx's default error: even though the API
                # key/secret travel via headers (not the URL) here, keep the
                # sanitized-error shape used across providers so this stays
                # safe if the auth mechanism ever changes.
                raise ValueError(f"Alpaca HTTP {response.status_code} for {path}")
            return response.json()


_OCC = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


def _parse_occ(occ: str, snap: dict[str, Any]) -> dict[str, Any] | None:
    m = _OCC.match(occ)
    if m is None:
        return None
    iv = _float(snap.get("impliedVolatility"))
    delta = _float((snap.get("greeks") or {}).get("delta"))
    if iv is None or delta is None or iv <= 0:
        return None
    try:
        expiry = datetime.strptime(m.group(2), "%y%m%d").date()
    except ValueError:
        return None
    return {"symbol": occ, "type": "call" if m.group(3) == "C" else "put", "strike": int(m.group(4)) / 1000,
            "expiration": expiry, "iv": iv, "delta": delta}


class DataUnavailable(ValueError):
    """The provider answers, but not for this plan (a 403): the caller falls back or skips."""


#: Alpaca keys bars by ticker, so a reused ticker joins two companies (docs/research/2026-11-12-alpaca-reused-tickers.md).
LISTING_GAP_DAYS = 60


def cut_at_listing_gap(rows: list[dict[str, Any]], gap_days: int = LISTING_GAP_DAYS) -> tuple[list[dict[str, Any]], int]:
    """Keep only the segment after the last gap of ``gap_days`` or more calendar days. Returns (rows, gap found or 0)."""
    dated = [r for r in rows if r.get("date") is not None]
    cut_at, found = 0, 0
    for i in range(1, len(dated)):
        gap = (dated[i]["date"] - dated[i - 1]["date"]).days
        if gap >= gap_days:
            cut_at, found = i, gap
    return (dated[cut_at:] if found else rows), found


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
