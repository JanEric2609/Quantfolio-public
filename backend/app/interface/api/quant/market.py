"""Market intelligence API endpoints — price data, sentiment, macro, analyst."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.interface.api.quant._common import _sentiment_label
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.schemas import PriceEnsureRequest
from app.foundation import market as market_service
from app.foundation.auth import current_user
from app.foundation.providers.registry import build_provider_registry

router = APIRouter(tags=["quant-market"])
logger = logging.getLogger(__name__)


@router.post("/price-data/ensure")
def ensure_price_data(payload: PriceEnsureRequest, db: Session = Depends(get_db), _user: User = Depends(current_user)) -> dict[str, Any]:
    rows = market_service.history(db, payload.ticker, days=payload.days)
    return {
        "ticker": payload.ticker.upper(),
        "rows": len(rows),
        "source": "cache_then_fetch",
        "stale": all(row.get("stale") for row in rows) if rows else True,
    }


@router.get("/market/sentiment/{ticker}")
def market_sentiment(
    ticker: str,
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    registry = build_provider_registry(db)
    result = registry.get_news(symbol=ticker, limit=limit)
    articles = result.get("data") or []
    provider = result.get("provider", "unknown")
    ok = result.get("ok", False)

    if not ok or not articles:
        from datetime import UTC, datetime, timedelta
        from app.foundation.models.entities import NewsItem

        cutoff = datetime.now(UTC) - timedelta(days=14)
        db_articles = (
            db.query(NewsItem)
            .filter(
                NewsItem.ticker == ticker.upper(),
                NewsItem.published_at >= cutoff,
            )
            .order_by(NewsItem.published_at.desc())
            .limit(limit)
            .all()
        )
        if db_articles:
            articles = [
                {
                    "title": a.title,
                    "summary": a.summary or "",
                    "url": a.url,
                    "source": a.source,
                    "published_at": a.published_at.isoformat(),
                    # Pass the nullable label through as-is: NULL means FinBERT
                    # hasn't scored this row yet, which is distinct from a
                    # genuine "neutral" score — collapsing the two here made
                    # every unscored article silently look neutral downstream.
                    "sentiment_label": a.sentiment_label,
                    "sentiment_score": float(a.sentiment_score) if a.sentiment_score is not None else None,
                }
                for a in db_articles
            ]
            provider = "news_db"

    total = len(articles)
    positive = sum(
        1
        for a in articles
        if _sentiment_label(a) in ("positive", "bullish", "very positive", "very bullish")
    )
    negative = sum(
        1
        for a in articles
        if _sentiment_label(a) in ("negative", "bearish", "very negative", "very bearish")
    )
    # _sentiment_label() returns "" when no sentiment field is present at all
    # (i.e. sentiment_label is NULL) — track that as "unscored", separate from
    # a genuine FinBERT "neutral" verdict, so aggregate counts don't silently
    # misrepresent unscored articles as neutral ones.
    unscored = sum(1 for a in articles if _sentiment_label(a) == "")
    neutral = total - positive - negative - unscored

    return {
        "ticker": ticker.upper(),
        "provider": provider,
        "available": ok or bool(articles),
        "articles": articles,
        "summary": {
            "total": total,
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
            "unscored": unscored,
            "score": round((positive - negative) / max(total, 1), 3),
        },
        "warnings": result.get("quality", {}).get("warnings", []),
    }


@router.get("/market/macro")
def market_macro(
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    registry = build_provider_registry(db)
    result = registry.get_macro_indicators()
    indicators = result.get("data") or []
    if isinstance(indicators, dict):
        indicators = [
            {
                "indicator": key,
                "value": val.get("value") if isinstance(val, dict) else val,
                "date": (val.get("date") or val.get("timestamp")) if isinstance(val, dict) else None,
                "timestamp": (val.get("timestamp") or val.get("date")) if isinstance(val, dict) else None,
            }
            for key, val in indicators.items()
        ]
    provider = result.get("provider", "unknown")

    return {
        "provider": provider,
        "available": result.get("ok", False),
        "indicators": indicators,
        "count": len(indicators),
        "warnings": result.get("quality", {}).get("warnings", []),
    }


@router.get("/market/analyst/{ticker}")
def market_analyst(
    ticker: str,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    registry = build_provider_registry(db)
    result = registry.get_analyst_estimates(symbol=ticker)
    estimates = result.get("data") or []
    provider = result.get("provider", "unknown")

    summary: dict[str, Any] = {}
    if estimates and isinstance(estimates, list):
        buy_count = sum(
            1
            for e in estimates
            if str(e.get("recommendation", "")).lower() in ("buy", "strong buy", "overweight", "outperform")
        )
        sell_count = sum(
            1
            for e in estimates
            if str(e.get("recommendation", "")).lower() in ("sell", "strong sell", "underweight", "underperform")
        )
        hold_count = len(estimates) - buy_count - sell_count
        summary = {
            "total_analysts": len(estimates),
            "buy": buy_count,
            "hold": hold_count,
            "sell": sell_count,
        }

    return {
        "ticker": ticker.upper(),
        "provider": provider,
        "available": result.get("ok", False),
        "estimates": estimates,
        "summary": summary,
        "warnings": result.get("quality", {}).get("warnings", []),
    }
