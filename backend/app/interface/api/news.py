from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.interface.api.safe_endpoint import safe_endpoint
from app.foundation.core.db import get_db
from app.foundation.core.net import validate_outbound_url
from app.foundation.models.entities import NewsItem, User, UserNewsRelevance
from app.foundation.auth import current_user, require_admin
from app.foundation.news import portfolio_symbols, refresh_news_for_user


router = APIRouter(prefix="/api/news", tags=["news"])


class FeedTestRequest(BaseModel):
    url: str


def _serialize_news_item(item: NewsItem, user_rel: UserNewsRelevance | None = None) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "summary": item.summary,
        "url": item.url,
        "source": item.source,
        "ticker": item.ticker,
        "sentiment_score": float(item.sentiment_score) if item.sentiment_score is not None else None,
        "sentiment_label": item.sentiment_label,
        "relevance_score": float(user_rel.relevance_score) if user_rel and user_rel.relevance_score is not None else (float(item.relevance_score) if item.relevance_score is not None else None),
        "relevance_label": user_rel.relevance_label if user_rel else item.relevance_label,
        "relevance_reason": user_rel.relevance_reason if user_rel else item.relevance_reason,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
        "is_macro": bool(item.is_macro),
    }


@router.get("")
@safe_endpoint
def list_news(
    ticker: str | None = None,
    source: str | None = None,
    sentiment: str | None = None,
    relevance: str | None = None,
    macro: bool | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    query = (
        db.query(NewsItem, UserNewsRelevance)
        .outerjoin(
            UserNewsRelevance,
            (NewsItem.id == UserNewsRelevance.news_item_id) & (UserNewsRelevance.user_id == _user.id),
        )
        .filter(NewsItem.published_at >= datetime.now(UTC) - timedelta(days=21))
    )
    if ticker:
        query = query.filter(NewsItem.ticker == ticker.upper())
    if source:
        query = query.filter(NewsItem.source == source)
    if sentiment:
        query = query.filter(NewsItem.sentiment_label == sentiment.lower())
    if relevance:
        # Unscored items (both labels NULL) are excluded by the NULL
        # comparison, never error.
        query = query.filter(
            (UserNewsRelevance.relevance_label == relevance.lower())
            | ((UserNewsRelevance.relevance_label.is_(None)) & (NewsItem.relevance_label == relevance.lower()))
        )
    if macro is True:
        query = query.filter(NewsItem.is_macro.is_(True))
    elif macro is False:
        query = query.filter(NewsItem.is_macro.is_(False))
    # Ordering contract: scored before unscored (NULL = "unscored", listed
    # after scored); newest first within each group.
    scored_first = case(
        (
            func.coalesce(UserNewsRelevance.relevance_score, NewsItem.relevance_score).is_not(None),
            0,
        ),
        else_=1,
    )
    rows = (
        query.order_by(scored_first.asc(), NewsItem.published_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [_serialize_news_item(row[0], row[1]) for row in rows]


@router.post("/refresh")
@safe_endpoint
def refresh_news(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    return refresh_news_for_user(db, user.id)


@router.get("/status")
@safe_endpoint
def news_status(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    latest = db.query(NewsItem).order_by(NewsItem.fetched_at.desc()).first()
    symbols = portfolio_symbols(db, user.id)
    return {
        "cached": db.query(NewsItem).count(),
        "portfolio_symbols": symbols,
        "latest_fetched_at": latest.fetched_at if latest else None,
        "message": "Portfolio intelligence news is ready." if latest else "No cached news yet. Run a refresh from News.",
    }


@router.get("/ticker/{ticker}")
@safe_endpoint
def ticker_news(
    ticker: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    rows = (
        db.query(NewsItem)
        .filter(NewsItem.ticker == ticker.upper())
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [_serialize_news_item(row) for row in rows]


@router.get("/macro")
@safe_endpoint
def macro_news(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    rows = (
        db.query(NewsItem)
        .filter(NewsItem.is_macro.is_(True))
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [_serialize_news_item(row) for row in rows]


@router.get("/rss")
@safe_endpoint
def rss_feeds(
    refresh: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Return parsed RSS feed items from free financial news sources.

    Query params:
    - refresh: Force re-fetch even if cache is fresh (default: False).
               Admin-only: ignored for non-admin users to prevent cache-DoS.
    """
    from app.foundation.rss_provider import fetch_all_feeds, invalidate_cache

    if refresh and user.role == "admin":
        invalidate_cache()

    result = fetch_all_feeds(db)
    items = result.get("items", [])
    return {
        "items": items,
        "sources": result.get("sources", []),
        "errors": result.get("errors", []),
        "total": len(items),
    }


@router.get("/feeds")
@safe_endpoint
def list_feeds(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """The configured feeds, one row per URL with its health, and where the list comes from."""
    from app.foundation.rss_provider import feed_health, feed_source

    return {"feeds": feed_health(db), "source": feed_source(db)}


MAX_FEEDS = 30


class FeedListIn(BaseModel):
    urls: list[str] = Field(
        max_length=MAX_FEEDS,
        description="The full list after an add, edit or remove. Empty restores the built-in defaults.",
    )


@router.put("/feeds")
@safe_endpoint
def save_feeds(
    payload: FeedListIn,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Replace the feed list (stored in the ``rss_feed_urls`` setting). Every URL must be public http(s)."""
    from app.foundation.rss_provider import invalidate_cache
    from app.foundation.settings import upsert_public_settings

    urls: list[str] = []
    for raw in payload.urls:
        url = raw.strip()
        if not url or url in urls:
            continue
        if "," in url:
            raise HTTPException(status_code=422, detail=f"A feed URL cannot contain a comma: {url}")
        try:
            validate_outbound_url(url, allow_private=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{url}: {exc}") from exc
        urls.append(url)
    upsert_public_settings(db, {"rss_feed_urls": ",".join(urls)})
    invalidate_cache()
    from app.foundation.rss_provider import feed_health, feed_source

    return {"feeds": feed_health(db), "source": feed_source(db)}


@router.post("/feeds/test")
@safe_endpoint
def test_feed(
    payload: FeedTestRequest,
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Test a single RSS feed URL and return sample items."""
    from app.foundation.rss_provider import fetch_feed

    # SSRF guard: reject private/internal addresses for feed URLs
    try:
        validate_outbound_url(payload.url, allow_private=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result = fetch_feed(payload.url)
    return {
        "ok": result["ok"],
        "feed_title": result.get("feed_title", ""),
        "items": result.get("items", [])[:3],
        "error": result.get("error"),
    }
