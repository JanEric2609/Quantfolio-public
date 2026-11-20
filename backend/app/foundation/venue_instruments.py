"""What the German venues list: Xetra and Börse Frankfurt, by ISIN.

Deutsche Börse publishes "all tradable instruments" as a semicolon-delimited CSV
per venue (two header lines, then one row per instrument with an ``ISIN``
column). Scalable routes orders to gettex, EIX and Xetra and publishes no list of
its own, so a Frankfurt or Xetra listing is the nearest checkable proxy: it
raises a share outside the EU and the US from "low" to "medium" with the reason
"listed in Frankfurt; confirm at Scalable before ordering" (plan 2026-11-10,
WP3, decision D3).

Before the first refresh there is no list: every answer is "unknown", never "no".
The file's URL carries a content hash and changes, so it is read off the
Deutsche Börse download pages at refresh time.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.foundation.models.entities import IndexUniverseMember, VenueInstrument

logger = logging.getLogger(__name__)

BASE = "https://www.cashmarket.deutsche-boerse.com"
PAGES = {
    "xfra": f"{BASE}/cash-en/trading/Tradable-Instruments-Xetra/Downloads/frankfurt-downloads",
    "xetr": f"{BASE}/cash-en/trading/Tradable-Instruments-Xetra/Downloads/xetra-downloads",
}
_LINK = re.compile(r'href="([^"]*allTradableInstruments\.csv)"')
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
REFRESH_AFTER = timedelta(days=6)
VENUE_LABEL = "Frankfurt"


def find_csv_url(page_html: str) -> str | None:
    """The ``allTradableInstruments.csv`` link on a Deutsche Börse download page."""
    match = _LINK.search(page_html)
    if not match:
        return None
    href = match.group(1)
    return href if href.startswith("http") else f"{BASE}{href}"


def parse_csv(text: str) -> tuple[date | None, list[tuple[str, str | None, str | None]]]:
    """``(file date, [(isin, name, currency)])`` for the active instruments in *text*."""
    lines = text.splitlines()
    as_of: date | None = None
    for line in lines[:3]:
        if line.lower().startswith("date last update"):
            try:
                as_of = datetime.strptime(line.split(";")[1].strip(), "%d.%m.%Y").date()
            except (IndexError, ValueError):
                as_of = None
    header_at = next((i for i, line in enumerate(lines[:6]) if line.startswith("Product Status")), None)
    if header_at is None:
        return as_of, []
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_at:])), delimiter=";")
    rows: dict[str, tuple[str, str | None, str | None]] = {}
    for rec in reader:
        isin = (rec.get("ISIN") or "").strip().upper()
        if not _ISIN.match(isin) or (rec.get("Instrument Status") or "Active").strip() != "Active":
            continue
        currency = (rec.get("Currency") or rec.get("Trading Currency") or "").strip().upper()[:3] or None
        rows[isin] = (isin, (rec.get("Instrument") or "").strip()[:200] or None, currency)
    return as_of, list(rows.values())


def _fetch(url: str) -> str:
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def refresh(db: Session, fetch: Callable[[str], str] = _fetch, now: datetime | None = None) -> dict[str, int]:
    """Replace each venue's stored list from Deutsche Börse; a venue that fails keeps its old list."""
    counts: dict[str, int] = {}
    for venue, page in PAGES.items():
        try:
            url = find_csv_url(fetch(page))
            if url is None:
                logger.warning("venue_instruments: no CSV link on %s", page)
                continue
            as_of, rows = parse_csv(fetch(url))
            if not rows:
                logger.warning("venue_instruments: %s file had no usable rows", venue)
                continue
        except Exception:  # noqa: BLE001 - one venue failing never stops the other
            logger.exception("venue_instruments: %s refresh failed", venue)
            continue
        db.query(VenueInstrument).filter(VenueInstrument.venue == venue).delete(synchronize_session=False)
        db.bulk_insert_mappings(
            VenueInstrument.__mapper__,
            [{"venue": venue, "isin": i, "name": n, "currency": c, "as_of": as_of} for i, n, c in rows],
        )
        db.commit()
        counts[venue] = len(rows)
    return counts


def last_refresh(db: Session) -> date | None:
    from sqlalchemy import func

    return db.query(func.max(VenueInstrument.as_of)).scalar()


def needs_refresh(db: Session, today: date | None = None) -> bool:
    last = last_refresh(db)
    return last is None or ((today or datetime.now(UTC).date()) - last) >= REFRESH_AFTER


def isin_for_symbol(db: Session, symbol: str) -> str | None:
    """The ISIN of an index constituent from the universe table (OpenFIGI-mapped), if known."""
    row = (
        db.query(IndexUniverseMember.isin)
        .filter(IndexUniverseMember.symbol == symbol.upper())
        .first()
    )
    return row[0] if row else None


class VenueListings:
    """Per-ISIN answers from the stored lists."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._loaded: bool | None = None

    @property
    def loaded(self) -> bool:
        if self._loaded is None:
            self._loaded = self._db.query(VenueInstrument.isin).limit(1).first() is not None
        return self._loaded

    def listed(self, isin: str | None) -> bool | None:
        """True/False from the lists; None without a list or without an ISIN."""
        key = (isin or "").strip().upper()
        if not self.loaded or not _ISIN.match(key):
            return None
        return self._db.query(VenueInstrument.isin).filter(VenueInstrument.isin == key).first() is not None
