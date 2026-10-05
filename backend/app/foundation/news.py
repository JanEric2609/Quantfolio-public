"""Shared news service logic used by both the API endpoint and the worker scheduler.

Extracted from ``api/news.py`` so the background scheduler can auto-refresh
news without duplicating the provider-iteration and storage logic.
"""

import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.live_positions import live_positions, set_position_ticker
from app.foundation.models.entities import (
    Asset,
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
    if existing:
        existing.fetched_at = datetime.now(UTC)
        existing.ticker = existing.ticker or (symbol.upper() if symbol else None)
        return False

    db.add(
        NewsItem(
            title=str(title)[:300],
            summary=item.get("summary"),
            url=url,
            source=str(item.get("source") or item.get("publisher") or provider)[:80],
            ticker=symbol.upper() if symbol else None,
            sentiment_score=item.get("sentiment_score"),
            sentiment_label=item.get("sentiment_label"),
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


def _score_news_item(item: NewsItem, keywords: set[str]) -> tuple[float, str, set[str]] | None:
    """Score one item via cheap keyword matching; ``None`` when nothing matched.

    Unmatched items stay unscored (NULL relevance) instead of receiving junk
    0.0 rows — audit §5 junk-match fix.
    """
    text = f"{item.title or ''} {item.summary or ''}".upper()
    matched: set[str] = set()
    for kw in keywords:
        if kw in text:
            matched.add(kw)
    if not matched:
        return None

    score = min(len(matched) * 0.15, 1.0)
    if item.is_macro:
        score = min(score + 0.3, 1.0)

    if score >= 0.6:
        return score, "high", matched
    if score >= 0.3:
        return score, "medium", matched
    return score, "low", matched


def tag_relevance(db: Session, user_id: str) -> dict[str, Any]:
    """Tag untagged news items with relevance scores for *user_id*.

    Returns a dict with ``tagged``, ``high``, ``medium``, and ``low`` keys.
    Items with zero keyword/entity matches are skipped and keep NULL scores.
    """
    symbols = portfolio_symbols(db, user_id)
    keywords: set[str] = set()
    for sym in symbols:
        keywords.add(sym.upper())

    # Also collect ISINs from the synced positions and ETF name keywords for relevance
    for pos in live_positions(db, user_id, include_unreconciled=True):
        if pos.isin:
            keywords.add(pos.isin.upper())

    # Add ETF asset name keywords for better RSS news relevance matching
    if symbols:
        etf_assets = (
            db.query(Asset)
            .filter(Asset.symbol.in_(symbols), Asset.asset_type == "etf")
            .all()
        )
        for asset in etf_assets:
            if asset.name:
                keywords.update(_etf_keywords(asset.name))

    keywords.update({k.upper() for k in MACRO_DOMAINS})

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
            scored = _score_news_item(item, keywords)
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
