"""What Trading 212 offers: its instrument list, by ISIN, with each line's currency.

The Trading 212 sync stores ``GET /equity/metadata/instruments`` in
``broker_instruments`` at most once a day (the endpoint allows one call per
50 s). Readers ask three things of it: does Trading 212 list this ISIN (the
Discover badge, the monthly plan's broker choice), in which currency (its only
trading cost is a 0.15 % FX fee on a line not in EUR), and where to open it.

Before the first sync there is no list: every answer is "unknown", never "no".
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.foundation.models.entities import BrokerInstrument

SOURCE = "trading212"
REFRESH_AFTER = timedelta(days=1)
APP_URL = "https://app.trading212.com/"
# Public instrument pages exist per ``SYMBOL.MARKET`` (``invest/AAPL.US``);
# only the US form maps from the API ticker (``AAPL_US_EQ``) with certainty.
_INSTRUMENT_URL = "https://www.trading212.com/trading-instruments/invest/{symbol}.US"
_US_TICKER = re.compile(r"^([A-Z0-9.]{1,12})_US_EQ$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _isin(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if _ISIN.match(text) else None


def last_refresh(db: Session) -> datetime | None:
    value = db.query(func.max(BrokerInstrument.synced_at)).filter(BrokerInstrument.source == SOURCE).scalar()
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def needs_refresh(db: Session, now: datetime | None = None) -> bool:
    last = last_refresh(db)
    return last is None or (now or datetime.now(UTC)) - last >= REFRESH_AFTER


def replace_instruments(db: Session, body: Any, now: datetime | None = None) -> int:
    """Replace the stored list with *body* (the API's JSON list); returns the lines kept (no commit).

    A body with no usable line leaves the old list in place.
    """
    now = now or datetime.now(UTC)
    rows: dict[str, dict[str, Any]] = {}
    for item in body if isinstance(body, list) else []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip()[:40]
        isin = _isin(item.get("isin"))
        if not ticker or not isin:
            continue
        rows[ticker] = {
            "source": SOURCE, "ticker": ticker, "isin": isin,
            "name": str(item.get("name") or "").strip()[:200] or None,
            "type": str(item.get("type") or "").strip().upper()[:24] or None,
            "currency": str(item.get("currencyCode") or "").strip().upper()[:3] or None,
            "synced_at": now,
        }
    if not rows:
        return 0
    db.query(BrokerInstrument).filter(BrokerInstrument.source == SOURCE).delete(synchronize_session=False)
    db.bulk_insert_mappings(BrokerInstrument.__mapper__, list(rows.values()))
    return len(rows)


def instrument_types(db: Session, isins: set[str]) -> dict[str, str]:
    """ISIN -> Trading 212 type (ETF, STOCK, ...) for the ISINs asked for that the list knows."""
    if not isins:
        return {}
    rows = db.query(BrokerInstrument.isin, BrokerInstrument.type).filter(
        BrokerInstrument.source == SOURCE, BrokerInstrument.isin.in_(isins), BrokerInstrument.type.is_not(None),
    )
    return {isin: kind for isin, kind in rows if kind}


class Trading212Listings:
    """Per-ISIN answers from the stored list, each ISIN queried once."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._loaded: bool | None = None
        self._lines: dict[str, list[tuple[str, str | None]]] = {}

    @property
    def loaded(self) -> bool:
        if self._loaded is None:
            self._loaded = last_refresh(self._db) is not None
        return self._loaded

    def lines(self, isin: str | None) -> list[tuple[str, str | None]]:
        """``[(ticker, currency)]`` Trading 212 offers for *isin*, EUR lines first."""
        key = _isin(isin)
        if key is None or not self.loaded:
            return []
        if key not in self._lines:
            rows = self._db.query(BrokerInstrument.ticker, BrokerInstrument.currency).filter(
                BrokerInstrument.source == SOURCE, BrokerInstrument.isin == key,
            ).all()
            self._lines[key] = sorted(((t, c) for t, c in rows), key=lambda r: (r[1] != "EUR", r[0]))
        return self._lines[key]

    def listed(self, isin: str | None) -> bool | None:
        """True/False from the list; None without a list or without an ISIN."""
        if not self.loaded or _isin(isin) is None:
            return None
        return bool(self.lines(isin))

    def currency(self, isin: str | None) -> str | None:
        """The currency an order would trade in: EUR when any line is in EUR, else the first line's."""
        lines = self.lines(isin)
        return lines[0][1] if lines else None

    def url(self, isin: str | None) -> str:
        """The instrument's public page for a US line, else the web app (search the ISIN there)."""
        for ticker, _ in self.lines(isin):
            match = _US_TICKER.match(ticker)
            if match:
                return _INSTRUMENT_URL.format(symbol=match.group(1))
        return APP_URL
