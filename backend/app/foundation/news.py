"""Shared news service logic used by both the API endpoint and the worker scheduler.

Extracted from ``api/news.py`` so the background scheduler can auto-refresh
news without duplicating the provider-iteration and storage logic.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.live_positions import live_positions, set_position_ticker
from app.foundation.models.entities import (
    Asset,
    Fundamental,
    Holding,
    NewsItem,
    Portfolio,
    ProviderHealth,
    UserNewsRelevance,
    WatchlistItem,
)
from app.foundation.providers.registry import build_provider_registry
from app.foundation.rss_provider import fetch_all_feeds

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

def portfolio_symbols(db: Session, user_id: str, *, resolve_isins: bool = False) -> list[str]:
    """Return a deduplicated, sorted list of ticker symbols for *user_id*.

    Sources: portfolio holdings, watchlist items, and synced broker positions (DKB, Scalable).
    When *resolve_isins* is True, synced positions that only have an ISIN will
    be resolved to a ticker via yfinance (slow — only use during refresh).
    """
    symbols: set[str] = set()

    # Portfolio holdings
    for row in (
        db.query(Holding)
        .join(Portfolio, Holding.portfolio_id == Portfolio.id)
        .filter(Portfolio.user_id == user_id, Holding.ticker.is_not(None))
        .all()
    ):
        if row.ticker:
            symbols.add(row.ticker.upper())

    # Watchlist
    for row in (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user_id, WatchlistItem.ticker.is_not(None))
        .all()
    ):
        if row.ticker:
            symbols.add(row.ticker.upper())

    # Synced broker positions, DKB and Scalable (ISIN → ticker resolution).
    # Symbols, not values: a position awaiting reconciliation still counts.
    did_resolve = False
    for pos in live_positions(db, user_id, include_unreconciled=True):
        if pos.ticker:
            symbols.add(pos.ticker.upper())
        elif pos.isin and resolve_isins:
            resolved = _resolve_isin_to_ticker(pos.isin)
            if resolved:
                set_position_ticker(db, pos, resolved)
                symbols.add(resolved.upper())
                did_resolve = True
    if did_resolve:
        db.commit()

    return sorted(symbols)


def _resolve_isin_to_ticker(isin: str) -> str | None:
    """Try yfinance ISIN lookup. Returns ticker string or None."""
    try:
        import yfinance as yf
        info = yf.Ticker(isin).info
        return info.get("symbol") or None
    except Exception:
        return None


# Common stopwords to ignore when building ETF keywords from asset names.
_ETF_STOPWORDS: set[str] = {
    "ETF", "UCITS", "FUND", "FUNDS", "FONDS", "INC", "CORP", "LTD", "LLC",
    "AG", "SA", "NV", "PLC", "CO", "COMPANY", "AND", "THE", "OF", "FOR",
    "IN", "ON", "AT", "TO", "A", "AN", "IS", "ARE", "WAS", "WERE", "BE",
    "BEEN", "BEING", "HAVE", "HAS", "HAD", "DO", "DOES", "DID", "WILL",
    "WOULD", "COULD", "SHOULD", "MAY", "MIGHT", "MUST", "SHALL", "CAN",
    "WITH", "FROM", "BY", "ABOUT", "INTO", "THROUGH", "DURING", "BEFORE",
    "AFTER", "ABOVE", "BELOW", "BETWEEN", "UNDER", "AGAIN", "FURTHER",
    "THEN", "ONCE", "HERE", "THERE", "WHEN", "WHERE", "WHY", "HOW", "ALL",
    "ANY", "BOTH", "EACH", "FEW", "MORE", "MOST", "OTHER", "SOME", "SUCH",
    "NO", "NOR", "NOT", "ONLY", "OWN", "SAME", "SO", "THAN", "TOO", "VERY",
    "JUST", "ALSO", "NOW",
}


def _etf_keywords(asset_name: str) -> list[str]:
    """Split an asset name into meaningful keywords for RSS matching."""
    words = asset_name.split()
    keywords = []
    for w in words:
        clean = w.strip(".,;:!?()[]{}\"'").upper()
        if clean and clean not in _ETF_STOPWORDS and len(clean) > 1:
            keywords.append(clean)
    return keywords


def _match_rss_items(symbol: str, asset_name: str, rss_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return RSS items whose title or summary matches any ETF keyword (word-boundary aware).

    Uses ``\\b`` word boundaries to avoid false positives from generic substrings
    (e.g. *CORE* matching "a CORE issue" or *WORLD* matching "WORLD-class results").
    """
    keywords = _etf_keywords(asset_name)
    if not keywords:
        return []
    patterns = [re.compile(rf'\b{re.escape(kw)}\b') for kw in keywords]
    matched: list[dict[str, Any]] = []
    for item in rss_items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".upper()
        if any(p.search(text) for p in patterns):
            matched.append(item)
    return matched


# ---------------------------------------------------------------------------
# News storage
# ---------------------------------------------------------------------------

# news_items.url is unique (migration 0114); a btree entry must stay well
# under Postgres's ~2.7 kB limit, and no real article URL comes close.
MAX_NEWS_URL_CHARS = 2000


def store_news_item(db: Session, item: dict[str, Any], provider: str, symbol: str | None) -> bool:
    """Persist a single news item. Returns True if newly inserted (not a duplicate)."""
    url = item.get("url") or item.get("link")
    title = item.get("title") or item.get("headline")
    if not url or not title or len(str(url)) > MAX_NEWS_URL_CHARS:
        return False
    url = str(url)

    # The session does not autoflush, so a row added earlier in the same
    # refresh is invisible to the query below. EUNL.DE and IWDA.L are the
    # same fund and match the same RSS articles, and one feed can list an
    # article twice: both used to be inserted, after which every refresh
    # raised MultipleResultsFound on that URL.
    existing = next(
        (obj for obj in db.new if isinstance(obj, NewsItem) and obj.url == url),
        None,
    ) or db.query(NewsItem).filter(NewsItem.url == url).first()
    published = _parse_published_at(
        item.get("published_at")
        or item.get("datetime")
        or item.get("providerPublishTime")
        or item.get("published"),
    )
    attributed = symbol
    if symbol and names_company(db, symbol, str(title), item.get("summary")) is False:
        # The feed of `symbol` returned an article about someone else (a Qualcomm
        # piece under AAPL): it keeps `feed_ticker` but names no company of ours.
        attributed = None
    if existing:
        existing.fetched_at = datetime.now(UTC)
        existing.ticker = existing.ticker or (attributed.upper() if attributed else None)
        existing.feed_ticker = existing.feed_ticker or (symbol.upper() if symbol else None)
        return False

    db.add(
        NewsItem(
            title=str(title)[:300],
            summary=item.get("summary"),
            url=url,
            source=str(item.get("source") or item.get("publisher") or provider)[:80],
            ticker=attributed.upper() if attributed else None,
            sentiment_score=item.get("sentiment_score"),
            sentiment_label=item.get("sentiment_label"),
            sentiment_source="provider" if item.get("sentiment_label") else None,
            feed_ticker=symbol.upper() if symbol else None,
            published_at=published,
            fetched_at=datetime.now(UTC),
            is_macro=symbol is None,
        )
    )
    return True


def _parse_published_at(value: Any) -> datetime:
    """Best-effort parse of a published_at value into a UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        raw = value.strip()
        for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Provider health
# ---------------------------------------------------------------------------

def _upsert_provider_health(
    db: Session,
    provider: str,
    capability: str,
    *,
    ok: bool,
    message: str = "",
) -> None:
    """Upsert a ProviderHealth record for a provider capability."""
    existing = (
        db.query(ProviderHealth)
        .filter(
            ProviderHealth.provider == provider,
            ProviderHealth.capability == capability,
        )
        .one_or_none()
    )
    now = datetime.now(UTC)
    if existing:
        existing.available = ok
        existing.status = "ok" if ok else "error"
        existing.message = message
        existing.updated_at = now
        if ok:
            existing.last_success_at = now
        else:
            existing.last_error_at = now
    else:
        db.add(
            ProviderHealth(
                provider=provider,
                capability=capability,
                configured=True,
                available=ok,
                status="ok" if ok else "error",
                message=message,
                last_success_at=now if ok else None,
                last_error_at=now if not ok else None,
            )
        )


# ---------------------------------------------------------------------------
# Refresh orchestrator
# ---------------------------------------------------------------------------

def _batch_resolve_assets(db: Session, symbols: list[str]) -> dict[str, Asset]:
    """Resolve *symbols* to Asset rows with a fixed number of queries.

    Direct ``Asset.symbol`` match first; unresolved symbols fall back to
    Holding.ticker → Holding.isin → Asset.isin (e.g. a holding recorded under
    a non-canonical listing ticker while the Asset row stores the canonical
    XETRA symbol for the same ISIN).
    """
    if not symbols:
        return {}
    resolved: dict[str, Asset] = {}
    for asset in db.query(Asset).filter(Asset.symbol.in_(symbols)).all():
        if asset.symbol:
            resolved.setdefault(asset.symbol, asset)
    missing = [s for s in symbols if s not in resolved]
    if not missing:
        return resolved
    isin_by_symbol: dict[str, str] = {}
    for holding in (
        db.query(Holding)
        .filter(Holding.ticker.in_(missing), Holding.isin.isnot(None))
        .all()
    ):
        if holding.ticker and holding.isin:
            isin_by_symbol.setdefault(holding.ticker, holding.isin)
    if isin_by_symbol:
        assets_by_isin: dict[str, Asset] = {}
        for asset in (
            db.query(Asset)
            .filter(Asset.isin.in_(sorted(set(isin_by_symbol.values()))))
            .all()
        ):
            if asset.isin:
                assets_by_isin.setdefault(asset.isin, asset)
        for sym, isin in isin_by_symbol.items():
            if isin in assets_by_isin:
                resolved[sym] = assets_by_isin[isin]
    return resolved


MACRO_FEED_ITEMS = 60


def _store_macro_feed_items(db: Session, cached: list[dict[str, Any]]) -> int:
    """Keep the newest general-feed articles with no ticker (``feed_ticker`` null)."""
    # The 30-minute rss_refresh job keeps the feeds fetched in this process; a
    # news refresh never waits on the network for them.
    from app.foundation.rss_provider import cached_items

    items = cached or cached_items()
    stored = 0
    for item in items[:MACRO_FEED_ITEMS]:
        if store_news_item(db, item, "rss", None):
            stored += 1
    return stored


def refresh_news_for_user(
    db: Session,
    user_id: str,
    *,
    max_symbols: int = 12,
    news_per_symbol: int = 8,
) -> dict[str, Any]:
    """Fetch and store news for a user's portfolio symbols.

    This is the shared implementation called by both ``POST /api/news/refresh``
    and the background scheduler.

    ETF symbols are handled via RSS feed keyword-matching instead of company
    news providers. If no RSS items match, the symbol is skipped quietly.

    Returns a dict with ``created``, ``symbols``, ``failed``, and
    ``symbols_detail`` keys.
    """
    symbols = portfolio_symbols(db, user_id, resolve_isins=True)
    registry = build_provider_registry(db)

    created = 0
    failed: list[str] = []
    symbols_detail: list[dict[str, Any]] = []
    attempted = symbols or [None]

    # RSS feed cache for ETF keyword matching — fetch once, reuse for all ETF symbols
    _rss_items: list[dict[str, Any]] = []

    assets_by_symbol = _batch_resolve_assets(
        db, [s for s in attempted[:max_symbols] if s]
    )

    for symbol in attempted[:max_symbols]:
        # ETF fallback: use RSS keyword matching instead of company news
        if symbol:
            asset = assets_by_symbol.get(symbol)
            if asset and asset.asset_type == "etf":
                if not _rss_items:
                    rss_result = fetch_all_feeds(db)
                    _rss_items = rss_result.get("items", [])
                matched = _match_rss_items(symbol, asset.name, _rss_items)
                n_matched = len(matched)
                if matched:
                    for item in matched[:news_per_symbol]:
                        try:
                            if store_news_item(db, item, "rss", symbol):
                                created += 1
                        except Exception as exc:
                            logger.warning("Failed to store RSS news item for %s: %s", symbol, exc)
                    _upsert_provider_health(db, "rss", "news", ok=True)
                else:
                    _upsert_provider_health(
                        db, "rss", "news", ok=True, message="No RSS items matched"
                    )
                symbols_detail.append(
                    {
                        "symbol": symbol,
                        "status": "ok" if n_matched > 0 else "skipped",
                        "items": min(n_matched, news_per_symbol),
                        "provider": "rss",
                        "is_etf": True,
                        "message": (
                            f"ETF: {n_matched} RSS items matched"
                            if n_matched > 0
                            else "No RSS items matched"
                        ),
                    }
                )
                continue

        result = registry.get_news(symbol, limit=news_per_symbol)
        rows = result.get("data") if result.get("ok") else []
        provider_name = result.get("provider", "unknown")
        if not rows:
            message = (
                "; ".join(result.get("quality", {}).get("warnings", []))
                or result.get("error")
                or "No news returned."
            )
            failed.append(f"{symbol or 'market'}: {message}")
            _upsert_provider_health(
                db, provider_name, "news", ok=False, message=message
            )
            symbols_detail.append(
                {
                    "symbol": symbol or "market",
                    "status": "failed",
                    "items": 0,
                    "provider": provider_name,
                    "is_etf": False,
                    "message": message,
                }
            )
            continue
        _upsert_provider_health(db, provider_name, "news", ok=True)
        symbols_detail.append(
            {
                "symbol": symbol or "market",
                "status": "ok",
                "items": len(rows),
                "provider": provider_name,
                "is_etf": False,
                "message": "",
            }
        )
        for item in rows:
            try:
                if store_news_item(db, item, provider_name, symbol):
                    created += 1
            except Exception as exc:
                logger.warning("Failed to store news item for %s: %s", symbol or "market", exc)

    # Market-wide feed items (no holding named) were fetched but never kept, so
    # `is_macro` was false on every row. Store the newest ones symbol-less; the
    # tagger below marks them macro or drops their relevance as usual.
    try:
        created += _store_macro_feed_items(db, _rss_items)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Macro feed items not stored: %s", exc)

    try:
        db.commit()
    except Exception:
        db.rollback()
        created = 0

    # Audit §5: tagging must never fail the refresh itself.
    try:
        tag_relevance(db, user_id)
    except Exception as exc:
        logger.warning("Relevance tagging failed after news refresh for %s: %s", user_id, exc)

    # Fast-path sentiment enrichment on newly ingested/unscored news items
    try:
        from app.foundation.sentiment import enrich_news_items
        enrich_news_items(db, limit=100)
    except Exception as exc:
        logger.debug("Inline sentiment enrichment deferred to worker: %s", exc)

    return {
        "created": created,
        "symbols": [item for item in attempted if item],
        "failed": failed,
        "symbols_detail": symbols_detail,
    }


# ---------------------------------------------------------------------------
# Relevance tagging
# ---------------------------------------------------------------------------

MACRO_DOMAINS: set[str] = {
    "inflation",
    "interest rate",
    "ecb",
    "fed",
    "gdp",
    "recession",
    "employment",
    "trade war",
    "tariff",
    "energy",
    "oil",
    "regulation",
    "monetary policy",
    "fiscal policy",
    "cpi",
    "ppi",
    "pmi",
    "yield curve",
}


# ---------------------------------------------------------------------------
# Company matching (WP5b/5c, plan 2026-11-10)
#
# The old matcher tested ``kw in TEXT.upper()`` for every ticker, so the owner's
# tickers C, O, MA, LOW and ACC (and words like CASH, CORE) matched inside
# ordinary words: 434 of 724 items were "high", for reasons like "C, MA, O".
# Company terms are now the company's name (case-insensitive, whole words) and
# a ticker of four or more characters as a whole token in its original case.
# ---------------------------------------------------------------------------

_NAME_SUFFIXES: set[str] = {
    "INC", "CORP", "CORPORATION", "LTD", "LIMITED", "LLC", "AG", "SE", "SA", "NV", "N.V.", "PLC", "CO",
    "COMPANY", "HOLDINGS", "HOLDING", "GROUP", "THE", "CLASS", "A", "B", "C", "ORD", "ADR", "SPA", "AB", "ASA",
    "OYJ", "KGAA", "S.A.", "A.G.", "&",
}
# A first word that names a sector or a country, not a company: "Banco" alone matches every bank story.
_GENERIC_FIRST_WORDS: set[str] = {
    "BANK", "BANCO", "BANCA", "GLOBAL", "UNITED", "AMERICAN", "GENERAL", "FIRST", "NEW", "INTERNATIONAL",
    "NATIONAL", "ROYAL", "GRUPO", "DEUTSCHE", "SAINT", "ST", "CHINA", "JAPAN", "WESTERN", "SOUTHERN", "NORTHERN",
    "EASTERN", "ENERGY", "FINANCIAL", "CAPITAL", "REALTY", "TECHNOLOGIES", "TECHNOLOGY",
}
# Index phrases an ETF's name carries; the fund's news is about the index.
_INDEX_PHRASES: tuple[str, ...] = (
    "MSCI World", "MSCI ACWI", "MSCI Emerging Markets", "MSCI Europe", "MSCI EMU", "MSCI USA", "MSCI Japan",
    "FTSE All-World", "FTSE 100", "S&P 500", "S&P 400", "Nasdaq 100", "Euro Stoxx 50", "Stoxx Europe 600",
    "DAX", "Dow Jones", "Russell 2000", "Russell 1000", "MDAX", "CAC 40",
)
_SHORT_MACRO_CASE_SENSITIVE = {"ecb": ("ECB",), "fed": ("Fed", "FED"), "gdp": ("GDP",), "cpi": ("CPI",), "ppi": ("PPI",), "pmi": ("PMI",)}


@dataclass
class RelevanceMatcher:
    """Company terms (title/summary match) and macro terms, compiled once per tagging run."""

    company: list[tuple[str, re.Pattern[str]]]
    macro: list[tuple[str, re.Pattern[str]]]


def _clean_name_words(name: str) -> list[str]:
    words = [w.strip(".,;:!?()[]{}\"'") for w in name.split()]
    return [w for w in words if w and w.upper() not in _NAME_SUFFIXES]


def company_terms(name: str | None, symbol: str | None = None, isin: str | None = None) -> list[tuple[str, re.Pattern[str]]]:
    """Terms that identify a holding in a headline: its name, a 4+ character ticker, its ISIN."""
    terms: list[tuple[str, re.Pattern[str]]] = []
    if name:
        indexes = [p for p in _INDEX_PHRASES if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", name, re.IGNORECASE)]
        for phrase in indexes:
            terms.append((phrase, re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)))
        if not indexes:
            words = [w for w in _clean_name_words(name) if w.upper() not in _ETF_STOPWORDS]
            if words:
                take = words[:2] if (len(words[0]) < 5 or words[0].upper() in _GENERIC_FIRST_WORDS) and len(words) > 1 else words[:1]
                phrase = " ".join(take)
                if len(phrase) >= 4:
                    terms.append((phrase, re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)))
    if symbol:
        bare = symbol.split(".")[0]
        if len(bare) >= 4 and bare.isalnum():
            terms.append((bare, re.compile(rf"(?<![A-Za-z0-9]){re.escape(bare)}(?![A-Za-z0-9])")))  # case-sensitive
    if isin:
        terms.append((isin.upper(), re.compile(re.escape(isin.upper()))))
    return terms


def _macro_terms() -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for kw in sorted(MACRO_DOMAINS):
        if kw in _SHORT_MACRO_CASE_SENSITIVE:
            alt = "|".join(re.escape(c) for c in _SHORT_MACRO_CASE_SENSITIVE[kw])
            out.append((kw, re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])")))
        else:
            out.append((kw, re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)", re.IGNORECASE)))
    return out


def build_matcher(db: Session, user_id: str) -> RelevanceMatcher:
    """The user's holdings as company terms, plus the macro vocabulary."""
    symbols = portfolio_symbols(db, user_id)
    names: dict[str, str] = {}
    for asset in db.query(Asset).filter(Asset.symbol.in_(symbols)).all() if symbols else []:
        if asset.name and asset.symbol:
            names[asset.symbol.upper()] = asset.name
    company: list[tuple[str, re.Pattern[str]]] = []
    seen: set[str] = set()
    for sym in symbols:
        for term in company_terms(names.get(sym.upper()), sym):
            if term[0] not in seen:
                seen.add(term[0])
                company.append(term)
    for pos in live_positions(db, user_id, include_unreconciled=True):
        for term in company_terms(pos.name, pos.ticker, pos.isin):
            if term[0] not in seen:
                seen.add(term[0])
                company.append(term)
    return RelevanceMatcher(company=company, macro=_macro_terms())


def _score_news_item(item: NewsItem, matcher: RelevanceMatcher) -> tuple[float, str, set[str]] | None:
    """Score one item; ``None`` when nothing matched (it keeps NULL relevance).

    A company term in the title is high, in the summary only medium; a macro
    term alone is low. The matched terms are the stored reason.
    """
    title, summary = item.title or "", item.summary or ""
    in_title = {t for t, p in matcher.company if p.search(title)}
    in_summary = {t for t, p in matcher.company if p.search(summary)} - in_title
    if in_title:
        return 0.8, "high", in_title | in_summary
    if in_summary:
        return 0.5, "medium", in_summary
    macro = {t for t, p in matcher.macro if p.search(title) or p.search(summary)}
    if macro:
        return 0.2, "low", macro
    return None


def _known_name(db: Session, symbol: str) -> tuple[str | None, str | None]:
    """``(name, asset_type)`` of *symbol* from the asset table, else from the stored fundamentals payload."""
    asset = db.query(Asset).filter(Asset.symbol == symbol.upper()).first()
    if asset is not None and asset.name:
        return asset.name, asset.asset_type
    row = db.query(Fundamental).filter(Fundamental.ticker == symbol.upper()).first()
    if row is not None:
        try:
            name = json.loads(row.data_json or "{}").get("name")
        except ValueError:
            name = None
        if isinstance(name, str) and name.strip():
            return name.strip(), None
    return None, None


def names_company(db: Session, symbol: str, title: str, summary: str | None) -> bool | None:
    """Whether an article names the company behind *symbol*; ``None`` when no name is known to test against."""
    name, asset_type = _known_name(db, symbol)
    if name is None:
        return None
    if asset_type == "etf":
        return True  # ETF news was chosen by keyword upstream
    text_terms = company_terms(name, symbol)
    if not text_terms:
        return None
    return any(p.search(title) or p.search(summary or "") for _, p in text_terms)


_SYMBOL_NEWS_TRIED: dict[str, float] = {}
_SYMBOL_NEWS_RETRY_S = 3600.0


def fetch_symbol_news(db: Session, symbol: str, limit: int = 8) -> int:
    """Fetch and store one symbol's news (a stock page for a name the owner does not hold).

    Held names get their feed from the refresh job; others had ``recent_news: []``
    forever. At most one attempt per symbol an hour. Returns the articles stored.
    """
    key = symbol.upper()
    now = time.monotonic()
    if now - _SYMBOL_NEWS_TRIED.get(key, -_SYMBOL_NEWS_RETRY_S) < _SYMBOL_NEWS_RETRY_S:
        return 0
    _SYMBOL_NEWS_TRIED[key] = now
    result = build_provider_registry(db).get_news(key, limit=limit)
    rows = result.get("data") if result.get("ok") else []
    stored = 0
    for item in rows or []:
        try:
            if store_news_item(db, item, str(result.get("provider", "unknown")), key):
                stored += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_symbol_news: failed to store an item for %s: %s", key, exc)
    db.commit()
    if stored:
        try:
            from app.foundation.sentiment import enrich_news_items

            enrich_news_items(db, ticker=key, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_symbol_news: sentiment deferred to the worker: %s", exc)
    return stored


def retag_news(db: Session) -> dict[str, int]:
    """One-off after the WP5 deploy: re-attribute stored articles and clear every relevance tag.

    ``feed_ticker`` takes the old ``ticker`` (the feed's symbol); ``ticker`` stays
    only where the article names that company; ``user_news_relevance`` and the
    item-level relevance columns are cleared so the next refresh tags everything
    with the new matcher. Returns the counts.
    """
    moved = cleared = 0
    for item in db.query(NewsItem).yield_per(500):
        if item.feed_ticker is None and item.ticker:
            item.feed_ticker = item.ticker
            if names_company(db, item.ticker, item.title or "", item.summary) is False:
                item.ticker = None
                cleared += 1
            moved += 1
        item.relevance_score = None
        item.relevance_label = None
        item.relevance_reason = None
    db.query(UserNewsRelevance).delete(synchronize_session=False)
    db.commit()
    return {"feed_ticker_set": moved, "ticker_cleared": cleared}


def tag_relevance(db: Session, user_id: str) -> dict[str, Any]:
    """Tag untagged news items with relevance scores for *user_id*.

    Returns a dict with ``tagged``, ``high``, ``medium``, and ``low`` keys.
    Items with zero keyword/entity matches are skipped and keep NULL scores.
    """
    matcher = build_matcher(db, user_id)

    untagged = (
        db.query(NewsItem)
        .filter(
            ~db.query(UserNewsRelevance)
            .filter(UserNewsRelevance.user_id == user_id, UserNewsRelevance.news_item_id == NewsItem.id)
            .exists()
        )
        .order_by(NewsItem.fetched_at.desc())
        .limit(200)
        .all()
    )

    counts = {"tagged": 0, "high": 0, "medium": 0, "low": 0}

    for item in untagged:
        try:
            scored = _score_news_item(item, matcher)
        except Exception as exc:
            logger.warning("tag_relevance: failed to score news item %s: %s", item.id, exc)
            continue
        if scored is None:
            continue

        score, label, matched = scored
        reason = ", ".join(sorted(matched))
        db.add(
            UserNewsRelevance(
                user_id=user_id,
                news_item_id=item.id,
                relevance_score=score,
                relevance_label=label,
                relevance_reason=reason,
            )
        )
        # Read-path fallback for users without their own tag: first write
        # wins, never overwrite, never backfill untagged legacy rows.
        if item.relevance_score is None:
            # NewsItem.relevance_score is a Numeric column (Decimal-typed);
            # the scorer yields floats, so convert before ORM assignment.
            item.relevance_score = Decimal(str(score))
            item.relevance_label = label
            item.relevance_reason = reason

        counts[label] += 1
        counts["tagged"] += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        counts = {"tagged": 0, "high": 0, "medium": 0, "low": 0}

    return counts
