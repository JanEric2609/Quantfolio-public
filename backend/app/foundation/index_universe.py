"""MSCI World constituents as a Discover universe source (plan 2026-11-08, §6).

The static index lists in ``discover/universe.py`` hold 770 unique stocks:
the STOXX 600 list has 68 names, the Nikkei 27 and the FTSE 65. The full MSCI
World (about 1,250 large and mid caps in 23 developed markets) is published
daily by its tracking funds; ``etf_currency`` already downloads the SPDR file
for currency weights. Its rows carry ISIN, name, trade country, currency,
weight and the GICS sector and industry, but no ticker.

:func:`refresh_members` (weekly, from the nightly prefetch job):

1. parses the file (:func:`parse_holdings`);
2. keeps every member's known Yahoo symbol: the one already mapped, else
   one the app already holds for that ISIN (``fundamentals`` cache);
3. maps the rest through OpenFIGI, asking for the listing on the member's
   home exchange (:data:`HOME_EXCHANGES`), paced under the free tier's
   25 requests a minute, and converts it with :func:`yahoo_symbol`;
4. drops rows that left the index.

A mapped symbol is only a guess until Yahoo returns prices for it: the
prefetch clears ``symbol_ok`` when it gets none, and such rows never enter the
universe (Singapore's SGX codes and some Nordic names do not match Bloomberg
tickers, for example). Why MSCI World and not every listed stock: breadth
helps the ranking tests (Ledoit, Wolf & Zhao 2019), while microcaps carry most
published anomalies and most of their trading costs (Hou, Xue & Zhang 2020;
Novy-Marx & Velikov 2016). MSCI World is large and mid caps only.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.etf_currency import _SPDR_FILES, _download, _xlsx_rows
from app.foundation.models.entities import Fundamental, IndexUniverseMember

logger = logging.getLogger(__name__)

INDEX_KEY = "msci_world"
#: Seconds between OpenFIGI requests: 25 a minute unauthenticated, kept under.
OPENFIGI_PAUSE_S = 3.0
OPENFIGI_BATCH = 10

#: Bloomberg exchange codes of each trade country's home listing, preferred first.
HOME_EXCHANGES: dict[str, tuple[str, ...]] = {
    "United States": ("US",),
    "Japan": ("JT", "JP"),
    "Canada": ("CN", "CT"),
    "United Kingdom": ("LN",),
    "France": ("FP",),
    "Germany": ("GY", "GR"),
    "Australia": ("AU", "AT"),
    "Sweden": ("SS",),
    "Switzerland": ("SW", "SE", "VX"),
    "Hong Kong": ("HK",),
    "Netherlands": ("NA",),
    "Italy": ("IM",),
    "Spain": ("SM", "SQ"),
    "Singapore": ("SP",),
    "Israel": ("IT",),
    "Denmark": ("DC",),
    "Finland": ("FH",),
    "Norway": ("NO",),
    "Belgium": ("BB",),
    "Austria": ("AV",),
    "Ireland": ("ID",),
    "Portugal": ("PL",),
    "New Zealand": ("NZ",),
}

#: Yahoo suffix per Bloomberg exchange code.
YAHOO_SUFFIX: dict[str, str] = {
    "US": "", "JT": ".T", "JP": ".T", "CN": ".TO", "CT": ".TO", "LN": ".L", "FP": ".PA",
    "GY": ".DE", "GR": ".DE", "AU": ".AX", "AT": ".AX", "SS": ".ST", "SW": ".SW", "SE": ".SW",
    "VX": ".SW", "HK": ".HK", "NA": ".AS", "IM": ".MI", "SM": ".MC", "SQ": ".MC", "SP": ".SI",
    "IT": ".TA", "DC": ".CO", "FH": ".HE", "NO": ".OL", "BB": ".BR", "AV": ".VI", "ID": ".IR",
    "PL": ".LS", "NZ": ".NZ",
}

#: Stockholm and Copenhagen tickers glue the share class on ("VOLVB"); Yahoo
#: writes "VOLV-B". Helsinki does not split them ("KNEBV.HE"), and splitting
#: there broke Elisa (trial of 2026-11-08), so only these two.
_CLASS_GLUED = {"SS", "DC"}
#: A 429 waits this long, then the batch is sent again (at most RETRIES times).
RATE_LIMIT_WAIT_S = 61.0
RETRIES = 2
_CLASS_RE = re.compile(r"\bClass\s+([A-Z])\b", re.IGNORECASE)


@dataclass(frozen=True)
class Holding:
    isin: str
    name: str
    country: str | None
    currency: str | None
    sector: str | None
    industry: str | None
    weight: float | None


def parse_holdings(content: bytes) -> tuple[list[Holding], date | None]:
    """Equity rows and the as-of date of an SPDR holdings file."""
    rows = _xlsx_rows(content)
    as_of: date | None = None
    header = None
    for i, row in enumerate(rows):
        label = (row.get("A") or "").strip().lower()
        if label.startswith("holdings as of") and row.get("B"):
            try:
                as_of = datetime.strptime(str(row["B"]).strip(), "%d-%b-%Y").date()
            except ValueError:
                as_of = None
        if row.get("A") == "ISIN":
            header = i
            break
    if header is None:
        raise ValueError("no ISIN header in holdings file")
    cols = {v: k for k, v in rows[header].items() if v}

    def cell(row: dict[str, str | None], name: str) -> str | None:
        key = cols.get(name)
        value = row.get(key) if key else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    out: list[Holding] = []
    for row in rows[header + 1:]:
        isin = (cell(row, "ISIN") or "").upper()
        if len(isin) != 12 or not isin[:2].isalpha():
            continue  # cash, futures and footer lines
        try:
            weight = float(cell(row, "Percent of Fund") or "")
        except ValueError:
            weight = None
        country = cell(row, "Trade Country Name")
        out.append(Holding(
            isin=isin,
            name=cell(row, "Security Name") or isin,
            country=None if country in (None, "None") else country,
            currency=(cell(row, "Currency") or "")[:3].upper() or None,
            sector=cell(row, "Sector Classification"),
            industry=cell(row, "Industry Classification"),
            weight=weight,
        ))
    return out, as_of


def yahoo_symbol(ticker: str, exch_code: str, name: str = "") -> str | None:
    """Yahoo symbol for a Bloomberg-style *ticker* on *exch_code*; None when the exchange is unknown.

    ``BRK/B`` -> ``BRK-B``, ``BP/`` -> ``BP.L``, ``5`` (HK) -> ``0005.HK``,
    ``VOLVB`` with "Class B" in the name -> ``VOLV-B.ST``.
    """
    suffix = YAHOO_SUFFIX.get(exch_code)
    if suffix is None or not ticker:
        return None
    t = ticker.strip().upper().rstrip("/").replace("/", "-").replace(" ", "-")
    if exch_code == "HK" and t.isdigit():
        t = t.zfill(4)
    if exch_code in _CLASS_GLUED and "-" not in t:
        match = _CLASS_RE.search(name or "")
        if match and len(t) > 1 and t.endswith(match.group(1).upper()):
            t = f"{t[:-1]}-{t[-1]}"
    return f"{t}{suffix}"


def _figi_post(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import os

    import requests

    headers = {"Content-Type": "application/json"}
    key = os.environ.get("OPENFIGI_API_KEY")
    if key:
        headers["X-OPENFIGI-APIKEY"] = key
    response = requests.post("https://api.openfigi.com/v3/mapping", json=jobs, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def map_symbols(
    holdings: list[Holding],
    *,
    post: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    pause: float = OPENFIGI_PAUSE_S,
) -> dict[str, str]:
    """``{isin: yahoo symbol}`` from OpenFIGI's home-exchange listing of each holding.

    One job per holding and exchange code, the preferred code first; a later
    code is tried only for holdings the earlier one did not find. Failures
    leave a holding unmapped (logged), never raise.
    """
    send = post or _figi_post
    out: dict[str, str] = {}
    pending = [h for h in holdings if h.country in HOME_EXCHANGES]
    first = True
    for rank in range(max(len(v) for v in HOME_EXCHANGES.values())):
        jobs = [(h, HOME_EXCHANGES[h.country][rank]) for h in pending  # type: ignore[index]
                if h.isin not in out and len(HOME_EXCHANGES[h.country]) > rank]  # type: ignore[index]
        for i in range(0, len(jobs), OPENFIGI_BATCH):
            batch = jobs[i:i + OPENFIGI_BATCH]
            if not first and pause:
                time.sleep(pause)
            first = False
            jobs_json = [{"idType": "ID_ISIN", "idValue": h.isin, "exchCode": code} for h, code in batch]
            results = None
            for attempt in range(RETRIES + 1):
                try:
                    results = send(jobs_json)
                    break
                except Exception as exc:  # noqa: BLE001 - one failed batch leaves its names unmapped
                    limited = getattr(getattr(exc, "response", None), "status_code", None) == 429
                    if limited and attempt < RETRIES:
                        time.sleep(RATE_LIMIT_WAIT_S if pause else 0)
                        continue
                    logger.warning("index_universe: OpenFIGI batch failed: %s", exc)
                    break
            if results is None:
                continue
            for (h, code), result in zip(batch, results):
                data = (result or {}).get("data") or []
                equity = [d for d in data if d.get("marketSector") == "Equity" and d.get("ticker")]
                if not equity:
                    continue
                symbol = yahoo_symbol(str(equity[0]["ticker"]), code, h.name)
                if symbol:
                    out[h.isin] = symbol
    return out


def _known_symbols(db: Session, isins: set[str]) -> dict[str, str]:
    """ISIN -> ticker from the fundamentals cache, for names the app already prices."""
    out: dict[str, str] = {}
    for ticker, raw in db.query(Fundamental.ticker, Fundamental.data_json).all():
        try:
            isin = (json.loads(raw or "{}") or {}).get("isin")
        except (TypeError, ValueError):
            continue
        if isinstance(isin, str) and isin.upper() in isins and ticker:
            out.setdefault(isin.upper(), ticker.upper())
    return out


def refresh_members(
    db: Session,
    *,
    fetch: Callable[[str], bytes] | None = None,
    post: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    pause: float = OPENFIGI_PAUSE_S,
) -> dict[str, Any]:
    """Reload MSCI World's members and map the new ones; the caller commits."""
    holdings, as_of = parse_holdings((fetch or _download)(_SPDR_FILES[INDEX_KEY]))
    now = datetime.now(UTC)
    existing = {m.isin: m for m in db.query(IndexUniverseMember).filter(IndexUniverseMember.index_key == INDEX_KEY)}
    current = {h.isin for h in holdings}
    removed = [m for isin, m in existing.items() if isin not in current]
    for m in removed:
        db.delete(m)
    need = [h for h in holdings if existing.get(h.isin) is None or existing[h.isin].symbol is None]
    known = _known_symbols(db, {h.isin for h in need})
    figi = map_symbols([h for h in need if h.isin not in known], post=post, pause=pause)
    for h in holdings:
        row = existing.get(h.isin)
        if row is None:
            row = IndexUniverseMember(index_key=INDEX_KEY, isin=h.isin, symbol_ok=True)
            db.add(row)
        row.name, row.country, row.currency = h.name[:200], h.country, h.currency
        row.sector, row.industry, row.weight, row.as_of = h.sector, h.industry, h.weight, as_of
        row.updated_at = now
        if row.symbol is None:
            symbol, source = (known[h.isin], "known") if h.isin in known else (figi.get(h.isin), "openfigi")
            if symbol:
                row.symbol, row.symbol_source, row.mapped_at, row.symbol_ok = symbol, source, now, True
    db.flush()
    report = {
        "as_of": as_of.isoformat() if as_of else None,
        "members": len(holdings),
        "removed": len(removed),
        "mapped_known": sum(1 for h in need if h.isin in known),
        "mapped_openfigi": len(figi),
        "unmapped": sum(1 for h in need if h.isin not in known and h.isin not in figi),
    }
    logger.info("index_universe refresh: %s", report)
    return report


def universe_symbols(db: Session, index_key: str = INDEX_KEY) -> list[IndexUniverseMember]:
    """Current members with a symbol Yahoo has priced (or not yet checked), largest weight first."""
    rows = (
        db.query(IndexUniverseMember)
        .filter(
            IndexUniverseMember.index_key == index_key,
            IndexUniverseMember.symbol.isnot(None),
            IndexUniverseMember.symbol_ok.is_(True),
        )
        .all()
    )
    return sorted(rows, key=lambda m: -(m.weight or 0.0))


def mark_checked(db: Session, symbol: str, ok: bool) -> None:
    """Record whether Yahoo returned prices for a member's symbol; the caller commits."""
    now = datetime.now(UTC)
    for row in db.query(IndexUniverseMember).filter(IndexUniverseMember.symbol == symbol.upper()):
        row.symbol_ok = ok
        row.checked_at = now


__all__ = [
    "HOME_EXCHANGES",
    "INDEX_KEY",
    "Holding",
    "map_symbols",
    "mark_checked",
    "parse_holdings",
    "refresh_members",
    "universe_symbols",
    "yahoo_symbol",
]
