"""Headlines about the non-US members of MSCI World, from GDELT (plan 2026-11-09, news).

The nightly AI reading of headlines had a source for US names only: Finnhub's
free company news refuses European listings, and Yahoo's news endpoint was
down. GDELT reads the world's online news (and machine-translates the
non-English part) and publishes, every 15 minutes, a file listing each article
with the organisations it names, its tone, its outlet and its title. Free, no
key, no rate limit.

What this keeps. An article counts for a member only when GDELT found the
company among the article's organisations *and* the company's name is in the
title, so the article is about the company rather than mentioning it in
passing. One row per (member, day, title) in ``news_mentions``: the same wire
story on fifty sites is one headline, not fifty.

Why the raw files and not GDELT's search API. The API answers one query every
5 seconds and only for the last 3 months; one pass over a day of files covers
every member at once. The cost is the download: about 96 English and 96
translated files a day, a few megabytes each, read as a stream and dropped.

Only the newest files since the last run are read (at most
:data:`MAX_HOURS`): the headline reading is forward-only, so a backfill would
not be used.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import sys
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import NewsMention
from app.foundation.providers.utils import is_us_listing

logger = logging.getLogger(__name__)

FILE_URL = "http://data.gdeltproject.org/gdeltv2/{stamp}.{kind}"
KINDS = ("gkg.csv.zip", "translation.gkg.csv.zip")
#: Hours of files read on the first run and after a gap.
MAX_HOURS = 30
#: Mentions older than this are deleted.
KEEP_DAYS = 90
THROUGH_KEY = "gdelt_through"

# GKG 2.1 columns.
_COL_DATE, _COL_OUTLET, _COL_URL, _COL_ORGS, _COL_TONE, _COL_EXTRAS = 1, 3, 4, 13, 15, 26
_TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)

#: Words that say what kind of company it is, not which one.
_LEGAL = {
    "ag", "sa", "se", "nv", "plc", "inc", "corp", "corporation", "co", "ltd", "limited", "spa", "asa",
    "ab", "oyj", "as", "a/s", "kgaa", "holding", "holdings", "group", "the", "class", "cl", "a", "b",
    "c", "ord", "reg", "adr", "sca", "bv", "sab", "de", "cv", "kk", "na", "npv", "pref", "ltd.",
}
#: First words too common to stand for one company on their own.
_GENERIC_FIRST = {
    "royal", "national", "first", "general", "united", "international", "deutsche", "bank", "banco",
    "china", "japan", "canadian", "american", "new", "great", "east", "west", "credit", "swiss",
    "british", "london", "hong", "nippon", "tokyo", "the", "global", "energy", "power", "next",
    "societe", "compagnie", "banque", "banca", "industrial", "industries", "bank", "south", "north",
    "central", "pacific", "asia", "euro", "europe", "western", "eastern", "standard", "capital",
}


def normalize(text: str) -> str:
    """Lower case, accents dropped, punctuation turned into spaces."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9/&]+", " ", plain).split())


def _words(name: str) -> list[str]:
    return [w for w in normalize(name).split() if w not in _LEGAL]


def aliases(name: str) -> set[str]:
    """Names a company goes by in the news: its name without legal words, and a distinctive first word."""
    words = _words(name)
    out: set[str] = set()
    if words:
        out.add(" ".join(words))
        if len(words) > 1 and len(words[0]) >= 4 and words[0] not in _GENERIC_FIRST:
            out.add(words[0])
    return {a for a in out if len(a) >= 3}


def alias_index(members: Iterable[tuple[str, str]]) -> dict[str, str]:
    """``{alias: symbol}`` from (symbol, name) pairs.

    A member's full name wins over another member's first word (SIEMENS AG
    keeps "siemens" although SIEMENS HEALTHINEERS starts with it); a first
    word two members share is dropped.
    """
    full: dict[str, set[str]] = {}
    first: dict[str, set[str]] = {}
    for symbol, name in members:
        words = _words(name)
        if not words:
            continue
        full.setdefault(" ".join(words), set()).add(symbol)
        for alias in aliases(name) - {" ".join(words)}:
            first.setdefault(alias, set()).add(symbol)
    out = {a: next(iter(s)) for a, s in full.items() if len(s) == 1 and len(a) >= 3}
    for alias, syms in first.items():
        if alias not in full and len(syms) == 1:
            out[alias] = next(iter(syms))
    return out


def _org_key(org: str) -> str:
    return " ".join(w for w in normalize(org).split() if w not in _LEGAL)


def _in_title(alias: str, title_norm: str, title_raw: str) -> bool:
    if len(alias) <= 3:
        # A three-letter name (SAP, ABB) counts only written in capitals.
        return re.search(rf"\b{re.escape(alias.upper())}\b", title_raw) is not None
    return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", title_norm) is not None


@dataclass(frozen=True)
class Mention:
    symbol: str
    seen_at: datetime
    outlet: str
    url: str
    title: str
    title_key: str
    tone: float | None


def parse_rows(lines: Iterable[str], index: dict[str, str], source: str) -> Iterator[tuple[Mention, str]]:
    """``(mention, source)`` for each article whose organisations and title name a member."""
    for parts in csv.reader(lines, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(parts) <= _COL_EXTRAS or not parts[_COL_ORGS]:
            continue
        orgs = {_org_key(o) for o in parts[_COL_ORGS].split(";") if o}
        hits = [index[o] for o in orgs if o in index]
        if not hits:
            continue
        match = _TITLE_RE.search(parts[_COL_EXTRAS])
        title = " ".join((match.group(1) if match else "").split())[:300]
        if not title:
            continue
        title_norm = normalize(title)
        try:
            seen_at = datetime.strptime(parts[_COL_DATE][:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        try:
            tone: float | None = float(parts[_COL_TONE].split(",")[0])
        except (ValueError, IndexError):
            tone = None
        by_symbol: dict[str, str] = {}
        for org in orgs:
            sym = index.get(org)
            if sym and _in_title(org, title_norm, title):
                by_symbol.setdefault(sym, org)
        for sym in by_symbol:
            yield Mention(
                symbol=sym, seen_at=seen_at, outlet=parts[_COL_OUTLET][:120], url=parts[_COL_URL],
                title=title, title_key=title_norm[:200], tone=tone,
            ), source


def _stamps(start: datetime, end: datetime) -> list[str]:
    """15-minute file stamps after *start* up to *end*."""
    t = start.replace(second=0, microsecond=0)
    t += timedelta(minutes=15 - t.minute % 15) if t.minute % 15 else timedelta(minutes=15)
    out: list[str] = []
    while t <= end:
        out.append(t.strftime("%Y%m%d%H%M%S"))
        t += timedelta(minutes=15)
    return out


def _download(url: str) -> bytes | None:
    import httpx

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.get(url)
    if response.status_code != 200:
        return None
    return response.content


def _lines(payload: bytes) -> Iterator[str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for member in zf.namelist():
            with zf.open(member) as fh:
                yield from io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")


def _members(db: Session) -> list[tuple[str, str]]:
    from app.foundation.index_universe import universe_symbols

    return [(m.symbol, m.name) for m in universe_symbols(db) if m.symbol and m.name and not is_us_listing(m.symbol)]


def refresh_gdelt_news(
    db: Session,
    *,
    now: datetime | None = None,
    fetch: Callable[[str], bytes | None] = _download,
) -> dict[str, Any]:
    """Read the GDELT files since the last run and store the members' headlines."""
    from app.foundation.settings import get_public_settings, upsert_public_settings

    clock = (now or datetime.now(UTC)).astimezone(UTC)
    members = _members(db)
    if not members:
        return {"status": "no_members"}
    index = alias_index(members)
    floor = clock - timedelta(hours=MAX_HOURS)
    try:
        start = datetime.strptime(str(get_public_settings(db).get(THROUGH_KEY) or ""), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        start = floor
    start = max(start, floor)
    # GDELT publishes a file a few minutes after its stamp.
    stamps = _stamps(start, clock - timedelta(minutes=20))
    stats: dict[str, Any] = {"members": len(members), "aliases": len(index), "files": 0, "missing": 0, "mentions": 0}
    pending: dict[tuple[str, date, str], tuple[Mention, str]] = {}
    # The watermark stops before the first slice with a file that did not
    # come back, so a failed download is retried next night. MAX_HOURS still
    # bounds the retry: a file GDELT never publishes is given up after 30 h.
    complete_through: str | None = None
    gap = False
    for stamp in stamps:
        for kind in KINDS:
            payload = fetch(FILE_URL.format(stamp=stamp, kind=kind))
            if payload is None:
                stats["missing"] += 1
                gap = True
                continue
            stats["files"] += 1
            try:
                for mention, source in parse_rows(_lines(payload), index, "gdelt" if kind == KINDS[0] else "gdelt_tr"):
                    pending.setdefault((mention.symbol, mention.seen_at.date(), mention.title_key), (mention, source))
            except (zipfile.BadZipFile, csv.Error) as exc:
                logger.info("gdelt: %s %s unreadable: %s", stamp, kind, exc)
        if not gap:
            complete_through = stamp
    stats["mentions"] = _store(db, pending)
    if complete_through is not None:
        upsert_public_settings(db, {THROUGH_KEY: complete_through})
    db.query(NewsMention).filter(NewsMention.day < clock.date() - timedelta(days=KEEP_DAYS)).delete()
    db.commit()
    logger.info("gdelt refresh: %s", stats)
    return {"status": "ok", **stats}


def _store(db: Session, pending: dict[tuple[str, date, str], tuple[Mention, str]]) -> int:
    if not pending:
        return 0
    days = {d for _, d, _ in pending}
    have = {
        (s, d, k)
        for s, d, k in db.query(NewsMention.symbol, NewsMention.day, NewsMention.title_key).filter(NewsMention.day.in_(days))
    }
    written = 0
    for key, (m, source) in pending.items():
        if key in have:
            continue
        db.add(NewsMention(
            symbol=m.symbol, day=key[1], seen_at=m.seen_at, source=source, outlet=m.outlet or None,
            url=m.url, title=m.title, title_key=m.title_key, gdelt_tone=m.tone,
        ))
        written += 1
    db.commit()
    return written


def headlines_between(db: Session, symbols: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, str]]]:
    """``{symbol: [(seen_at, title), ...]}`` of the mentions seen in [start, end)."""
    if not symbols:
        return {}
    rows = (
        db.query(NewsMention.symbol, NewsMention.seen_at, NewsMention.title)
        .filter(NewsMention.symbol.in_(symbols), NewsMention.day >= start.date(), NewsMention.day <= end.date())
        .all()
    )
    out: dict[str, list[tuple[datetime, str]]] = {}
    for sym, seen_at, title in rows:
        when = seen_at if seen_at.tzinfo else seen_at.replace(tzinfo=UTC)
        if start <= when < end:
            out.setdefault(sym, []).append((when, title))
    return out


# GKG rows can carry very long fields (GCAM); the csv module's default cap is 128 KiB.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

__all__ = ["alias_index", "aliases", "headlines_between", "normalize", "parse_rows", "refresh_gdelt_news"]
