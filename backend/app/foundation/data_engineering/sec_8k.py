"""8-K filings of the US names we price, with their item codes (plan 2026-11-09, news).

An 8-K is the filing a US company must make within four business days of a
material event, and its item codes say which kind: 4.02 "financial statements
can no longer be relied on", 3.01 a delisting notice, 5.02 a director or
officer leaving, 1.03 bankruptcy, 2.06 an impairment, 4.01 a change of
auditor. Unlike a headline these are the company's own, dated to the second
(``acceptanceDateTime``), free and complete.

Two places use them:

* the nightly AI reading of headlines (``lab.forward_lab.news_tone``): each
  material item filed in the reading window is one more line for the model to
  rate, so a restatement is read even on a day the news feed missed it;
* Discover's dossier facts: a material 8-K in the last 30 days is a fact the
  dossier must be able to cite (:func:`recent_material_text`).

How. EDGAR's daily form index lists every 8-K filed that day with its CIK;
SEC's ``company_tickers.json`` maps our tickers to CIKs. For each CIK with an
8-K that day one call to ``data.sec.gov/submissions`` returns the item codes
of its recent filings. The insider-trading job already reads the daily index
at SEC's rate limit, so this runs right after it on the same client.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.data_engineering.sec_edgar_insider import (
    DAILY_INDEX,
    SecClient,
    _edgar_closed,
    parse_form_index,
    user_agent,
)
from app.foundation.models.entities import Sec8kEvent
from app.foundation.providers.utils import is_us_listing

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FORMS = ("8-K", "8-K/A")
#: Days read on the first run, and the most read in one night after a gap.
FIRST_RUN_DAYS = 30
#: An index younger than this that is missing is "not yet published", not "no filings".
UNPUBLISHED_GRACE = timedelta(days=7)
#: Internal state in ``app_settings``: the last filing day fully read.
THROUGH_KEY = "sec_8k_through"

#: Item codes worth a reader's attention, in plain words. Routine items are
#: left out on purpose: 2.02 (results: the numbers are in the press release,
#: not the code), 7.01 / 8.01 (anything), 9.01 (exhibits), 5.07 (votes).
MATERIAL_ITEMS: dict[str, str] = {
    "1.01": "entered a material agreement",
    "1.02": "ended a material agreement",
    "1.03": "filed for bankruptcy or receivership",
    "1.05": "reported a material cybersecurity incident",
    "2.01": "completed an acquisition or disposal of assets",
    "2.04": "had an event that accelerates a debt obligation",
    "2.05": "announced exit or restructuring costs",
    "2.06": "recorded a material impairment",
    "3.01": "received a delisting notice or failed a listing rule",
    "3.03": "materially modified the rights of its shareholders",
    "4.01": "changed its auditor",
    "4.02": "said its past financial statements can no longer be relied on",
    "5.01": "had a change in control",
    "5.02": "had a director or senior officer leave or join",
}
#: The items that are bad news nearly whatever the details.
RED_FLAG_ITEMS = frozenset({"1.03", "1.05", "2.04", "2.06", "3.01", "4.01", "4.02"})


def material_items(items: str) -> list[str]:
    """The material item codes in an ``items`` string such as ``"2.02,5.02,9.01"``."""
    return [code for code in (c.strip() for c in items.split(",")) if code in MATERIAL_ITEMS]


def describe(items: str) -> str | None:
    """``"the company changed its auditor; ..."``, or None when nothing material was filed."""
    codes = material_items(items)
    if not codes:
        return None
    return "; ".join(f"{MATERIAL_ITEMS[c]} (8-K item {c})" for c in codes)


def cik_map(client: SecClient, symbols: list[str]) -> dict[str, str]:
    """``{cik (10 digits): symbol}`` for the US *symbols* SEC knows."""
    status, body = client.get(TICKERS_URL)
    if status != 200:
        raise ValueError(f"SEC company_tickers.json HTTP {status}")
    by_ticker: dict[str, str] = {}
    for row in json.loads(body).values():
        ticker = str(row.get("ticker") or "").upper()
        if ticker and ticker not in by_ticker:
            by_ticker[ticker] = str(row.get("cik_str") or "").zfill(10)
    out: dict[str, str] = {}
    for sym in symbols:
        plain = sym.upper().replace(".", "-")  # Yahoo BRK-B, SEC BRK-B; some feeds BRK.B
        cik = by_ticker.get(plain) or by_ticker.get(sym.upper())
        if cik:
            out.setdefault(cik, sym)
    return out


def _accepted(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def filing_items(submissions: dict[str, Any]) -> dict[str, tuple[str, str, datetime | None]]:
    """``{accession: (form, items, accepted_at)}`` from a submissions JSON's recent filings."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    items = recent.get("items") or []
    accepted = recent.get("acceptanceDateTime") or []
    out: dict[str, tuple[str, str, datetime | None]] = {}
    for i, acc in enumerate(accessions):
        form = forms[i] if i < len(forms) else ""
        if form not in FORMS:
            continue
        out[str(acc)] = (
            str(form), str(items[i] if i < len(items) else "")[:120], _accepted(accepted[i] if i < len(accepted) else None),
        )
    return out


def _universe_us_symbols(db: Session) -> list[str]:
    """US members of the index universe and every stored US bar series."""
    from app.foundation.data_backbone.cross_check import stored_symbols
    from app.foundation.index_universe import universe_symbols

    syms = {m.symbol for m in universe_symbols(db) if m.symbol}
    syms |= set(stored_symbols(db))
    return sorted(s for s in syms if is_us_listing(s))


def _days(db: Session, today: date) -> list[date]:
    from app.foundation.settings import get_public_settings

    raw = str(get_public_settings(db).get(THROUGH_KEY) or "")
    try:
        start = date.fromisoformat(raw) + timedelta(days=1)
    except ValueError:
        start = today - timedelta(days=FIRST_RUN_DAYS)
    start = max(start, today - timedelta(days=FIRST_RUN_DAYS))
    closed = _edgar_closed(start, today)
    out: list[date] = []
    day = start
    while day < today:
        if day.weekday() < 5 and day not in closed:
            out.append(day)
        day += timedelta(days=1)
    return out


def ingest_day(
    db: Session, client: SecClient, day: date, ciks: dict[str, str], stats: dict[str, int],
) -> bool:
    """Store the day's 8-Ks of our names; False when EDGAR has no index for it."""
    quarter = (day.month - 1) // 3 + 1
    status, body = client.get(DAILY_INDEX.format(year=day.year, quarter=quarter, day=f"{day:%Y%m%d}"))
    if status != 200:
        return False
    entries = [e for e in parse_form_index(body.decode("latin-1"), FORMS) if e.cik in ciks]
    wanted: dict[str, list[str]] = {}
    for entry in entries:
        accession = entry.path.rsplit("/", 1)[-1].removesuffix(".txt")
        wanted.setdefault(entry.cik, []).append(accession)
    for cik, accessions in wanted.items():
        have = {a for (a,) in db.query(Sec8kEvent.accession).filter(Sec8kEvent.accession.in_(accessions))}
        if have >= set(accessions):
            continue
        status, raw = client.get(SUBMISSIONS_URL.format(cik=cik))
        if status != 200:
            stats["failed"] += 1
            continue
        try:
            known = filing_items(json.loads(raw))
        except (json.JSONDecodeError, AttributeError):
            stats["failed"] += 1
            continue
        for accession in accessions:
            if accession in have:
                continue
            form, items, accepted = known.get(accession, ("8-K", "", None))
            db.add(Sec8kEvent(
                accession=accession, symbol=ciks[cik], cik=cik, form=form,
                filed_on=day, accepted_at=accepted, items=items,
            ))
            stats["filings"] += 1
            if material_items(items):
                stats["material"] += 1
    db.commit()
    return True


def refresh_8k_events(
    db: Session, *, client: SecClient | None = None, today: date | None = None,
) -> dict[str, Any]:
    """Read every daily index since the last one read; returns counts."""
    from app.foundation.settings import upsert_public_settings

    clock = today or datetime.now(UTC).date()
    sec = client or SecClient(user_agent(db))
    symbols = _universe_us_symbols(db)
    if not symbols:
        return {"status": "no_symbols"}
    ciks = cik_map(sec, symbols)
    stats = {"symbols": len(symbols), "ciks": len(ciks), "days": 0, "filings": 0, "material": 0, "failed": 0}
    for day in _days(db, clock):
        if not ingest_day(db, sec, day, ciks, stats):
            if clock - day < UNPUBLISHED_GRACE:
                break  # not published yet; tomorrow's run reads it
            continue
        stats["days"] += 1
        upsert_public_settings(db, {THROUGH_KEY: day.isoformat()})
    logger.info("sec_8k refresh: %s", stats)
    return {"status": "ok", **stats}


def events_between(db: Session, symbols: list[str], start: datetime, end: datetime) -> dict[str, list[str]]:
    """``{symbol: [plain-language line, ...]}`` of material 8-Ks accepted in [start, end)."""
    if not symbols:
        return {}
    rows = (
        db.query(Sec8kEvent)
        .filter(Sec8kEvent.symbol.in_(symbols), Sec8kEvent.filed_on >= start.date() - timedelta(days=1))
        .filter(Sec8kEvent.filed_on <= end.date())
        .all()
    )
    out: dict[str, list[str]] = {}
    for row in rows:
        when = row.accepted_at
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when is None or not (start <= when < end):
            continue
        text = describe(row.items)
        if text:
            out.setdefault(row.symbol, []).append(f"The company {text}.")
    return out


def recent_material_text(db: Session, symbol: str, *, today: date | None = None, days: int = 30) -> str | None:
    """One dossier phrase for the material 8-Ks of the last *days*, or None."""
    clock = today or datetime.now(UTC).date()
    rows = (
        db.query(Sec8kEvent)
        .filter(Sec8kEvent.symbol == symbol, Sec8kEvent.filed_on >= clock - timedelta(days=days))
        .order_by(Sec8kEvent.filed_on.desc())
        .all()
    )
    parts: list[str] = []
    for row in rows:
        text = describe(row.items)
        if text:
            parts.append(f"on {row.filed_on.isoformat()} it {text}")
    if not parts:
        return None
    return "8-K filings in the last 30 days: " + "; ".join(parts[:3])


__all__ = [
    "MATERIAL_ITEMS",
    "RED_FLAG_ITEMS",
    "cik_map",
    "describe",
    "events_between",
    "filing_items",
    "material_items",
    "recent_material_text",
    "refresh_8k_events",
]
