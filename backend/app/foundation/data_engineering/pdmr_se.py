"""Managers' transactions (MAR Art. 19) from Sweden's Finansinspektionen register.

Why. Insider purchases are the strongest published signal among the free data a
European investor can reach (Afzali & Martikainen 2021 on 16 European countries;
Zingg et al. 2007 on Switzerland; Hvide & Nielsen on Norway). Germany's register (BaFin) forbids automated
reads on its portal and the issuers' notice service (EQS) forbids storing its content, so Sweden is the
first source whose licence allows this: FI's open-data page says its information may
be "used free of charge", "freely processed and re-used without special permission
or agreements", with the source (FI) and the date stated.

How. The register's public search lists every notification, newest first, ten per
page. Plain GET links page through it. A page's table is parsed with the standard
library only. A row's ``side`` is ``buy`` for "Acquisition" and ``sell`` for
"Disposal"; allotments, subscriptions, gifts, exercises and the like are ``other`` (never a signal).
An amended notification appears as ``Revised`` and ``History`` rows; they are one stored row.

Politeness. One request every :data:`DELAY_S` seconds, an identifying but anonymous
User-Agent, at most :data:`MAX_PAGES` pages a night. The first runs walk back to
:data:`BACKFILL_DAYS` over several nights from a saved page number; once caught up, a
night stops at the first page it already knows entirely.

Status. The table layout (headers, date and number formats) was read from the live
page on 2026-11-25; the raw HTML was not available, so the parser finds columns by
their header text and ignores everything else. A first run on the server confirms it:
``refresh_pdmr_se`` returns the page and row counts, and ``failed_pages`` is non-zero
when the layout differs.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.foundation.models.entities import PdmrTransaction

logger = logging.getLogger(__name__)

SOURCE = "fi_se"
PAGE_URL = (
    "https://marknadssok.fi.se/Publiceringsklient/en-GB/Search/Search/Insyn"
    "?button=search&SearchFunctionType=Insyn&language=en-gb&paging=True&page={page}"
)
USER_AGENT = "quantfolio-pdmr-reader/1.0 (personal research; low rate)"
DELAY_S = 2.0
MAX_PAGES = 400
BACKFILL_DAYS = 365
#: Internal state in ``app_settings``: the next page of an unfinished backfill.
RESUME_KEY = "pdmr_se_resume_page"

_HEADERS = {
    "publication date": "publication_date",
    "issuer": "issuer",
    "person discharging managerial responsibilities": "person",
    "position": "position",
    "closely associated": "closely_associated",
    "nature of transaction": "nature",
    "instrument name": "instrument_name",
    "intrument type": "instrument_type",  # sic, as the register spells it
    "instrument type": "instrument_type",
    "isin": "isin",
    "transaction date": "transaction_date",
    "volume": "volume",
    "unit": "unit",
    "price": "price",
    "currency": "currency",
    "status": "status",
}
_REQUIRED = ("publication_date", "issuer", "person", "nature", "isin", "transaction_date", "volume", "price")
_BUY = {"acquisition", "purchase"}
_SELL = {"disposal", "sale"}


@dataclass(frozen=True)
class Pdmr:
    issuer: str
    person: str
    position: str
    closely_associated: bool
    nature: str
    instrument_type: str
    isin: str | None
    transaction_date: date | None
    publication_date: date | None
    volume: float | None
    price: float | None
    currency: str
    status: str = ""

    @property
    def side(self) -> str:
        key = self.nature.strip().lower()
        return "buy" if key in _BUY else "sell" if key in _SELL else "other"

    @property
    def key(self) -> str:
        raw = "|".join(str(x) for x in (
            SOURCE, self.isin, self.person, self.nature, self.transaction_date,
            self.volume, self.price,
        ))
        return hashlib.sha1(raw.encode()).hexdigest()  # noqa: S324 - an identity hash, not security


class _Table(HTMLParser):
    """Collects the cell text of every table row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_date(raw: str) -> date | None:
    """``DD/MM/YYYY`` (the register's format) or ISO; ``None`` when blank or unreadable."""
    text = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(raw: str) -> float | None:
    """``18,337`` or ``31.00`` (comma thousands, period decimals); ``None`` when blank."""
    text = raw.strip().replace(",", "").replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_page(html: str) -> list[Pdmr]:
    """Rows of the results table on one page; ``[]`` when no table has the expected headers."""
    table = _Table()
    table.feed(html)
    columns: dict[str, int] | None = None
    out: list[Pdmr] = []
    for row in table.rows:
        mapped = {_HEADERS[c.strip().lower()]: i for i, c in enumerate(row) if c.strip().lower() in _HEADERS}
        if all(name in mapped for name in _REQUIRED):
            columns = mapped
            continue
        if columns is None:
            continue

        def cell(name: str, row: list[str] = row, columns: dict[str, int] = columns) -> str:
            index = columns.get(name)
            return row[index] if index is not None and index < len(row) else ""

        isin = cell("isin").strip().upper()
        if not cell("issuer") or not cell("person"):
            continue
        out.append(Pdmr(
            issuer=cell("issuer")[:200], person=cell("person")[:200], position=cell("position")[:120],
            closely_associated=cell("closely_associated").strip().lower() in ("yes", "ja"),
            nature=cell("nature")[:60], instrument_type=cell("instrument_type")[:60],
            isin=isin if len(isin) == 12 else None,
            transaction_date=parse_date(cell("transaction_date")),
            publication_date=parse_date(cell("publication_date")),
            volume=parse_number(cell("volume")), price=parse_number(cell("price")),
            currency=cell("currency").strip().upper()[:3],
            status=cell("status").strip()[:16],
        ))
    return out


def fetch_page(page: int, *, timeout_s: float = 30.0) -> str:
    """One results page as text; raises on any HTTP error."""
    import httpx

    response = httpx.get(
        PAGE_URL.format(page=page), headers={"User-Agent": USER_AGENT},
        timeout=timeout_s, follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def _superseded(status: str) -> bool:
    return status.strip().lower() == "history"


def store(db: Session, rows: list[Pdmr]) -> int:
    """Insert rows not stored yet; returns how many were new. Does not commit.

    The register lists an amended notification as separate ``Revised`` and ``History`` rows
    with the same transaction. They share one key, so one transaction is one row. Where a
    ``History`` copy was stored first and the ``Revised`` one arrives later, the stored row
    takes the later one's status and publication date.
    """
    keys: dict[str, Pdmr] = {}
    for r in rows:
        kept = keys.get(r.key)
        if kept is None or (_superseded(kept.status) and not _superseded(r.status)):
            keys[r.key] = r
    have = (
        {t.row_key: t for t in db.query(PdmrTransaction).filter(PdmrTransaction.row_key.in_(list(keys)))}
        if keys else {}
    )
    new = 0
    for key, r in keys.items():
        stored = have.get(key)
        if stored is not None:
            if _superseded(stored.status) and not _superseded(r.status):
                stored.status, stored.publication_date = r.status, r.publication_date
            continue
        db.add(PdmrTransaction(
            row_key=key, source=SOURCE, isin=r.isin, issuer=r.issuer, person=r.person,
            position=r.position, closely_associated=r.closely_associated, side=r.side,
            nature=r.nature, instrument_type=r.instrument_type,
            transaction_date=r.transaction_date, publication_date=r.publication_date,
            volume=r.volume, price=r.price, currency=r.currency, status=r.status,
        ))
        new += 1
    return new


def refresh_pdmr_se(
    db: Session, *, fetch: Callable[[int], str] = fetch_page,
    sleep: Callable[[float], None] = time.sleep, today: date | None = None,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Read new pages of the register; returns counts (pages, rows, new, failed_pages, done)."""
    from app.foundation.settings import get_public_settings, upsert_public_settings

    clock = today or datetime.now(UTC).date()
    cutoff = clock - timedelta(days=BACKFILL_DAYS)
    saved = get_public_settings(db).get(RESUME_KEY)
    try:
        page = max(int(saved), 1) if saved else 1
    except (TypeError, ValueError):
        page = 1
    backfilling = page > 1
    stats: dict[str, Any] = {"pages": 0, "rows": 0, "new": 0, "failed_pages": 0, "done": False}
    for _ in range(max_pages):
        try:
            rows = parse_page(fetch(page))
        except Exception as exc:  # noqa: BLE001 - network or layout; resume here next night
            logger.warning("pdmr_se page %s failed: %s", page, type(exc).__name__)
            stats["failed_pages"] += 1
            break
        stats["pages"] += 1
        if not rows:
            # an empty parse of a fetched page: layout changed (or the end of the list)
            stats["failed_pages"] += 1
            break
        stats["rows"] += len(rows)
        added = store(db, rows)
        db.commit()
        stats["new"] += added
        oldest = min((r.publication_date for r in rows if r.publication_date), default=None)
        if oldest is not None and oldest < cutoff:
            stats["done"] = True
            break
        if added == 0 and not backfilling:
            stats["done"] = True  # caught up: this whole page was already stored
            break
        page += 1
        sleep(DELAY_S)
    resume = "" if stats["done"] or page == 1 else str(page)
    upsert_public_settings(db, {RESUME_KEY: resume})
    return stats


def recent_buys(db: Session, isin: str, *, days: int = 90, today: date | None = None) -> list[PdmrTransaction]:
    """Acquisitions of shares published for *isin* in the last *days* days, newest first.

    Excludes superseded (``History``) copies and every instrument type other than a share.
    **Limit:** the register's list page has no venue column, so an acquisition made outside a
    trading venue (a private deal, a conversion) is still returned; only the notification's
    detail page says where it was executed. Treat the result as "reported acquisitions", not
    as proven open-market purchases, until that is checked.
    """
    start = (today or datetime.now(UTC).date()) - timedelta(days=days)
    return (
        db.query(PdmrTransaction)
        .filter(
            PdmrTransaction.isin == isin, PdmrTransaction.side == "buy",
            func.lower(PdmrTransaction.instrument_type) == "share",
            func.lower(PdmrTransaction.status) != "history",
            PdmrTransaction.publication_date >= start,
        )
        .order_by(PdmrTransaction.publication_date.desc())
        .all()
    )
