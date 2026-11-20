"""Free real-time news sources, read only (ADR 0027 amendment 2026-11-20).

Six sources, all free and checked live on 2026-11-20:

* **Alpaca news stream** (Benzinga): ``wss://stream.data.alpaca.markets/v1beta1/news``.
  The free plan's key may subscribe to every symbol (``"*"``); items arrive
  seconds after publication, including European companies. One connection per
  key, so only the worker opens it.
* **Federal Reserve** press releases (RSS), **ECB** press releases, speeches and
  decisions (RSS): polled once a minute.

The RSS feeds get a neutral User-Agent (``FEED_USER_AGENT``); only SEC gets the
configured one, which SEC requires to carry a contact.
* **finanznachrichten.de ad-hoc feed** (RSS): every German ad-hoc disclosure
  (Art. 17 MAR: profit warnings, takeovers, management exits) about a minute
  after release, each item tagged with the company's ISIN.
* **Handelsblatt headlines** (RSS): German business and market headlines.
  Headlines only; the articles behind them stay paywalled and are never read.
* **SEC EDGAR** latest 8-K filings (Atom, ``getcurrent``), polled once a minute
  inside SEC's 10 requests/second limit with the configured User-Agent.

This module fetches and parses; deciding what matters and telling the owner is
the desk's news wire (``decision/desk/newswire.py``). Nothing here can trade.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

from defusedxml import ElementTree

logger = logging.getLogger(__name__)

ALPACA_NEWS_STREAM = "wss://stream.data.alpaca.markets/v1beta1/news"
FEEDS: dict[str, str] = {
    "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ecb": "https://www.ecb.europa.eu/rss/press.html",
    "de_adhoc": "https://www.finanznachrichten.de/rss-aktien-adhoc",
    "handelsblatt": "https://www.handelsblatt.com/contentexport/feed/schlagzeilen",
}
FEED_USER_AGENT = "news-wire-reader/1.0"
_FN_ISIN = "{http://www.finanznachrichten.de/service/rss}isin"
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
SEC_CURRENT_8K = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom"
_ATOM = "{http://www.w3.org/2005/Atom}"
_SEC_TITLE = re.compile(r"^(?P<form>[^ ]+) - (?P<name>.+?) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)$")
_SEC_ITEM = re.compile(r"Item (\d+\.\d+)")


@dataclass(frozen=True)
class WireItem:
    source: str  # alpaca | fed | ecb | de_adhoc | handelsblatt | sec_8k
    source_id: str
    headline: str
    published_at: datetime | None
    url: str | None = None
    #: Tickers (Alpaca) or ISINs (the German ad-hoc feed), matched against the watchlist.
    symbols: tuple[str, ...] = ()
    #: SEC only: the filer's CIK and the 8-K item codes ("2.02", "5.02", ...).
    cik: str | None = None
    items: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        """``source:source_id``; a longer one (an article URL) is hashed, never cut, so two
        articles whose URLs share a long prefix stay two items."""
        raw = f"{self.source}:{self.source_id}"
        return raw if len(raw) <= 96 else f"{self.source}:h:{hashlib.sha256(self.source_id.encode()).hexdigest()}"


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def parse_rss(body: bytes, source: str) -> list[WireItem]:
    """RSS 2.0 items (Fed, ECB, German ad-hoc, Handelsblatt). Malformed XML raises ``ElementTree.ParseError``."""
    root = ElementTree.fromstring(body)
    out: list[WireItem] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip() or None
        guid = (item.findtext("guid") or "").strip() or link or title
        if not title:
            continue
        isins = tuple(dict.fromkeys(i for el in item.findall(_FN_ISIN) if _ISIN.match(i := (el.text or "").strip().upper())))
        out.append(WireItem(source, guid, unescape(title)[:400], _parse_time(item.findtext("pubDate")), link, isins))
    return out


def parse_sec_current(body: bytes) -> list[WireItem]:
    """EDGAR's latest-filings Atom feed: one item per filer entry (subject companies are skipped)."""
    root = ElementTree.fromstring(body)
    out: list[WireItem] = []
    for entry in root.iter(f"{_ATOM}entry"):
        title = unescape((entry.findtext(f"{_ATOM}title") or "").strip())
        m = _SEC_TITLE.match(title)
        if not m or m.group("role") != "Filer":
            continue
        link_el = entry.find(f"{_ATOM}link")
        summary = unescape(entry.findtext(f"{_ATOM}summary") or "")
        out.append(WireItem(
            "sec_8k", (entry.findtext(f"{_ATOM}id") or title).rsplit("=", 1)[-1],
            f"{m.group('form')}: {m.group('name')}"[:400], _parse_time(entry.findtext(f"{_ATOM}updated")),
            link_el.get("href") if link_el is not None else None, cik=m.group("cik"),
            items=tuple(_SEC_ITEM.findall(summary)),
        ))
    return out


def alpaca_item(msg: dict[str, Any]) -> WireItem | None:
    """One ``{"T": "n", ...}`` stream message, or None for control messages."""
    if msg.get("T") != "n" or not msg.get("headline"):
        return None
    return WireItem(
        "alpaca", str(msg.get("id")), str(msg["headline"])[:400], _parse_time(msg.get("created_at")),
        str(msg.get("url") or "") or None, tuple(str(s).upper() for s in (msg.get("symbols") or [])[:20]),
    )


def fetch(url: str, user_agent: str, *, timeout_s: float = 15.0) -> bytes:
    """GET *url*; raises ``ValueError`` on a non-200 answer."""
    import httpx

    response = httpx.get(url, headers={"User-Agent": user_agent}, timeout=timeout_s, follow_redirects=True)
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    return response.content


class StreamRefused(RuntimeError):
    """Alpaca refused the stream (bad key, or another connection already holds it)."""


async def alpaca_stream(key: str, secret: str) -> AsyncIterator[WireItem]:
    """Yield news items from Alpaca's stream until the connection drops (the caller reconnects)."""
    import websockets

    async with websockets.connect(ALPACA_NEWS_STREAM, ping_interval=20, ping_timeout=20,
                                  open_timeout=20, close_timeout=5) as ws:
        await ws.recv()  # [{"T":"success","msg":"connected"}]
        await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        auth = json.loads(await ws.recv())
        if not any(m.get("T") == "success" and m.get("msg") == "authenticated" for m in auth):
            # e.g. {"T":"error","code":406,"msg":"connection limit exceeded"}; never log the key.
            codes = [m.get("code") for m in auth if m.get("T") == "error"]
            raise StreamRefused(f"auth refused {codes}")
        await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        async for raw in ws:
            try:
                batch = json.loads(raw)
            except (TypeError, ValueError):
                continue
            for msg in batch if isinstance(batch, list) else [batch]:
                if isinstance(msg, dict) and (item := alpaca_item(msg)) is not None:
                    yield item
                await asyncio.sleep(0)
