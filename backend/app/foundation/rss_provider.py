"""Free RSS news feed provider.

Fetches and parses RSS feeds from free financial news sources. Caches
results in-memory with a 30-minute TTL to avoid hammering upstream servers.
No API keys required for basic RSS feeds.

Default sources:
- Reuters Business
- MarketWatch
- Yahoo Finance
- Investopedia
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as DefusedTree
import httpx

from app.foundation.core.net import pin_url_to_ip, validate_outbound_url

logger = logging.getLogger(__name__)

# In-memory cache: {feed_url: {"items": [...], "fetched_at": float}}
_feed_cache: dict[str, dict[str, Any]] = {}
CACHE_TTL_SECONDS = 1800  # 30 minutes

DEFAULT_FEED_URLS: list[str] = [
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",  # MarketWatch
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",  # CNBC Top
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",  # CNBC Markets
    "https://finance.yahoo.com/news/rssindex",  # Yahoo Finance
    "https://www.tagesschau.de/wirtschaft/index~rss2.xml",  # tagesschau German
    "https://www.ecb.europa.eu/rss/press.html",  # ECB press
    "https://www.federalreserve.gov/feeds/press_all.xml",  # Fed press
]

# HTTP timeout for feed fetching (seconds)
FEED_TIMEOUT = 15


def get_default_feed_urls() -> list[str]:
    """Return the built-in default RSS feed URLs."""
    return list(DEFAULT_FEED_URLS)


def get_feed_urls(db=None) -> list[str]:
    """Resolve feed URLs: DB setting → env var → built-in defaults.

    If *db* is provided, checks the ``rss_feed_urls`` AppSetting first.
    The setting value is a comma-separated list of URLs.
    """
    env_urls = None
    import os
    env_var = os.environ.get("RSS_FEED_URLS")
    if env_var:
        env_urls = [u.strip() for u in env_var.split(",") if u.strip()]

    if db is not None:
        try:
            from app.foundation.settings import get_setting_row
            db_value = get_setting_row(db, "rss_feed_urls")
            if db_value and isinstance(db_value, str) and db_value.strip():
                db_urls = [u.strip() for u in db_value.split(",") if u.strip()]
                if db_urls:
                    return db_urls
        except Exception:
            logger.debug("Could not read rss_feed_urls from DB, falling through")

    if env_urls:
        return env_urls

    return get_default_feed_urls()


def fetch_feed(url: str, *, timeout: int = FEED_TIMEOUT) -> dict[str, Any]:
    """Fetch and parse a single RSS feed URL.

    Returns a dict with ``ok``, ``items``, ``feed_title``, and ``error`` keys.
    Items are dicts with: title, link, summary, published, source.
    """
    cached = _feed_cache.get(url)
    if cached and (time.time() - cached["fetched_at"]) < CACHE_TTL_SECONDS:
        return {
            "ok": True,
            "items": cached["items"],
            "feed_title": cached.get("feed_title", ""),
            "cached": True,
        }

    try:
        url, resolved_ip = validate_outbound_url(url, allow_private=False)
    except ValueError as exc:
        logger.warning("SSRF blocked for RSS feed %s: %s", url, exc)
        return {"ok": False, "items": [], "feed_title": "", "error": f"URL validation failed: {exc}", "cached": False}

    try:
        # Pin the actual connection to the IP address we just validated —
        # httpx.get(url, ...) would otherwise re-resolve the hostname itself,
        # reopening a DNS-rebinding TOCTOU window between validation and fetch.
        # extensions= is only accepted by Client.get()/httpx.request(), not
        # the module-level httpx.get() convenience function.
        pinned_url, hostname, extensions = pin_url_to_ip(url, resolved_ip)
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(pinned_url, follow_redirects=False, headers={
                "Host": hostname,
                "User-Agent": "QuantFolio-RSS/1.0",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            }, extensions=extensions)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to fetch RSS feed %s: %s", url, exc)
        return {"ok": False, "items": [], "feed_title": "", "error": str(exc), "cached": False}

    return parse_feed_xml(resp.text, url)


def parse_feed_xml(xml_text: str, source_url: str = "") -> dict[str, Any]:
    """Parse RSS/Atom XML text into structured feed items.

    Returns a dict with ``ok``, ``items``, ``feed_title``, and ``error`` keys.
    """
    items: list[dict[str, Any]] = []
    feed_title = ""

    try:
        root = DefusedTree.fromstring(xml_text)
    except DefusedTree.ParseError as exc:
        logger.warning("Failed to parse RSS XML from %s: %s", source_url, exc)
        return {"ok": False, "items": [], "feed_title": "", "error": f"XML parse error: {exc}", "cached": False}

    # Detect feed format
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # RSS 2.0: <rss><channel><title>...<item>...
    channel = root.find("channel")
    if channel is not None:
        feed_title = _text(channel, "title")
        for item_el in channel.findall("item"):
            item = _parse_rss_item(item_el, source_url)
            if item:
                items.append(item)
    else:
        # Atom: <feed><title>...<entry>...
        feed_title_el = root.find("atom:title", ns)
        if feed_title_el is None:
            feed_title_el = root.find("title")
        feed_title = feed_title_el.text.strip() if feed_title_el is not None and feed_title_el.text else ""
        entries = root.findall("atom:entry", ns)
        if not entries:
            entries = root.findall("entry")
        for entry_el in entries:
            item = _parse_atom_entry(entry_el, source_url, ns)
            if item:
                items.append(item)

    if not items:
        return {"ok": False, "items": [], "feed_title": feed_title, "error": "No items found in feed", "cached": False}

    # Cache the result
    _feed_cache[source_url] = {
        "items": items,
        "feed_title": feed_title,
        "fetched_at": time.time(),
    }

    return {"ok": True, "items": items, "feed_title": feed_title, "cached": False}


def feed_source(db=None) -> str:
    """Where :func:`get_feed_urls` took the list from: ``settings``, ``env`` or ``default``."""
    import os

    if db is not None:
        try:
            from app.foundation.settings import get_setting_row

            value = get_setting_row(db, "rss_feed_urls")
            if isinstance(value, str) and any(u.strip() for u in value.split(",")):
                return "settings"
        except Exception:
            logger.debug("Could not read rss_feed_urls from DB")
    if any(u.strip() for u in os.environ.get("RSS_FEED_URLS", "").split(",")):
        return "env"
    return "default"


def feed_health(db=None) -> list[dict[str, Any]]:
    """One row per configured URL: ``{url, title, status, items, error}`` (fetched in parallel)."""
    urls = get_feed_urls(db)
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as executor:
        results = list(executor.map(fetch_feed, urls))
    return [
        {
            "url": url,
            "title": r.get("feed_title") or "",
            "status": "ok" if r.get("ok") else "error",
            "items": len(r.get("items") or []),
            "error": None if r.get("ok") else r.get("error"),
        }
        for url, r in zip(urls, results, strict=True)
    ]


def fetch_all_feeds(db=None) -> dict[str, Any]:
    """Fetch all configured RSS feeds and return combined results.

    Returns ``{"items": [...], "sources": {...}, "errors": [...]}``.
    """
    urls = get_feed_urls(db)
    all_items: list[dict[str, Any]] = []
    sources: dict[str, int] = {}
    errors: list[str] = []

    executor = ThreadPoolExecutor(max_workers=min(len(urls), 8))
    try:
        futures = {executor.submit(fetch_feed, url): url for url in urls}
        done, not_done = wait(futures, timeout=20.0)

        for future in done:
            url = futures[future]
            try:
                result = future.result()
                if result["ok"]:
                    feed_title = result.get("feed_title") or url
                    sources[feed_title] = len(result["items"])
                    all_items.extend(result["items"])
                else:
                    errors.append(f"{url}: {result.get('error', 'Unknown error')}")
            except Exception as exc:
                errors.append(f"{url}: {exc}")

        for future in not_done:
            url = futures[future]
            errors.append(f"{url}: Timed out after 20s")
            future.cancel()
    finally:
        executor.shutdown(wait=False)

    # Sort by published date (most recent first)
    all_items.sort(key=lambda x: x.get("published", ""), reverse=True)

    return {
        "items": all_items,
        "sources": sources,
        "errors": errors,
    }


def invalidate_cache(url: str | None = None) -> None:
    """Invalidate the feed cache. If url is None, clear all cached feeds."""
    if url:
        _feed_cache.pop(url, None)
    else:
        _feed_cache.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _text(el: Element, tag: str) -> str:
    """Extract text content from an XML element."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _text_ns(el: Element, tag: str, ns: dict) -> str:
    """Extract text content using namespace-aware find."""
    child = el.find(f"{{{ns.get('atom', '')}}}{tag}")
    return (child.text or "").strip() if child is not None else ""


def _parse_rss_item(item_el: Element, source_url: str) -> dict[str, Any] | None:
    """Parse an RSS 2.0 <item> element."""
    title = _text(item_el, "title")
    link = _text(item_el, "link")
    if not title and not link:
        return None

    summary = _text(item_el, "description")
    published = _text(item_el, "pubDate")
    source = _text(item_el, "source") or _extract_domain(source_url)

    return {
        "title": title[:300],
        "link": link,
        "summary": _clean_html(summary)[:500] if summary else "",
        "published": published,
        "source": source[:80],
    }


def _parse_atom_entry(entry_el: Element, source_url: str, ns: dict) -> dict[str, Any] | None:
    """Parse an Atom <entry> element."""
    title = _text_ns(entry_el, "title", ns) or _text(entry_el, "title")
    link_el = entry_el.find("{http://www.w3.org/2005/Atom}link")
    if link_el is None:
        link_el = entry_el.find("link")
    link = link_el.get("href", "") if link_el is not None else ""
    if not title and not link:
        return None

    summary_el = entry_el.find("{http://www.w3.org/2005/Atom}summary")
    if summary_el is None:
        summary_el = entry_el.find("summary")
    content_el = entry_el.find("{http://www.w3.org/2005/Atom}content")
    if content_el is None:
        content_el = entry_el.find("content")
    summary = ""
    if summary_el is not None and summary_el.text:
        summary = summary_el.text
    elif content_el is not None and content_el.text:
        summary = content_el.text

    published_el = entry_el.find("{http://www.w3.org/2005/Atom}published")
    if published_el is None:
        published_el = entry_el.find("published")
    updated_el = entry_el.find("{http://www.w3.org/2005/Atom}updated")
    if updated_el is None:
        updated_el = entry_el.find("updated")
    published = ""
    if published_el is not None and published_el.text:
        published = published_el.text
    elif updated_el is not None and updated_el.text:
        published = updated_el.text

    return {
        "title": title[:300],
        "link": link,
        "summary": _clean_html(summary)[:500] if summary else "",
        "published": published,
        "source": _extract_domain(source_url)[:80],
    }


def _clean_html(text: str) -> str:
    """Strip HTML tags from text (simple regex-free approach)."""
    import re
    clean = re.sub(r"<[^>]+>", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _extract_domain(url: str) -> str:
    """Extract domain name from a URL for use as feed source label."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host:
            return "Unknown"
        parts = host.split(".")
        if len(parts) >= 2:
            return parts[-2].title()
        return parts[0].title() if parts else "Unknown"
    except Exception:
        return "Unknown"
