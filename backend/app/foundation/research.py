"""Stock Research Hub — orchestrator service.

Assembles price history, TA indicators, fundamentals, news sentiment,
existing recommendations, portfolio context, regime state, and the
user's risk profile into a context payload.  Sends it to the LLM to
produce a structured research report that is cached 24 h in the DB.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.portfolio.name_resolver import resolve_position_name
from app.foundation.models.entities import (
    MultiHorizonVerdict,
    NewsItem,
    PriceCache,
    StockResearchReport,
    User,
)
from app.foundation.market import fundamentals, quote
from app.foundation.text_safety import ascii_safe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Report TTL
# ---------------------------------------------------------------------------
REPORT_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Data-gathering helpers (pure, testable)
# ---------------------------------------------------------------------------

def _price_history(db: Session, ticker: str, days: int = 365) -> list[dict[str, Any]]:
    """Pull OHLCV rows from PriceCache."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        db.query(PriceCache)
        .filter(PriceCache.ticker == ticker, PriceCache.date >= cutoff)
        .order_by(PriceCache.date)
        .all()
    )
    return [{"date": str(r.date), "open": float(r.open) if r.open is not None else 0,
             "high": float(r.high) if r.high is not None else 0,
             "low": float(r.low) if r.low is not None else 0,
             "close": float(r.close) if r.close is not None else 0,
             "volume": int(r.volume or 0)} for r in rows]


def _latest_fundamentals(db: Session, ticker: str) -> dict[str, Any] | None:
    """Fundamentals for the research view and prompt.

    Goes through :func:`market.fundamentals`, which re-fetches a row past
    its TTL or marked stale. Reading the table directly served rows that
    migration 0113 marked stale for their wrong units: EXI2.DE's dividend
    yield of 0.34 (a percent; the fund yields about 0.34 %) went to the
    research model as a 34 % yield.
    """
    try:
        result = fundamentals(db, ticker)
    except Exception:
        logger.warning("research: fundamentals lookup failed for %s", ticker, exc_info=True)
        return None
    data = result.get("data")
    return data if isinstance(data, dict) and data else None


def _recent_news(db: Session, ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = (
        db.query(NewsItem)
        .filter(NewsItem.ticker == ticker)
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
        .all()
    )
    return [{"title": r.title, "source": r.source, "published_at": str(r.published_at),
             "sentiment_label": r.sentiment_label, "sentiment_score": float(r.sentiment_score or 0)} for r in rows]


def _existing_recommendations(db: Session, ticker: str) -> list[dict[str, Any]]:
    from app.foundation.models.entities import Recommendation
    rows = (
        db.query(Recommendation)
        .filter(Recommendation.ticker == ticker)
        .order_by(Recommendation.created_at.desc())
        .limit(5)
        .all()
    )
    # ``confidence`` is on different scales per mode: 0-100 for the long-term
    # recommender, a 0-1 ranking score (not a probability) for Discover. The
    # mode travels with it so the reader can tell a 0.61 from a 61.
    return [{"verdict": r.verdict, "confidence": float(r.confidence), "mode": r.mode,
             "horizon": r.horizon, "created_at": str(r.created_at)} for r in rows]


def _portfolio_context(db: Session, user_id: str) -> dict[str, Any]:
    from app.foundation.models.entities import Holding, Portfolio
    holdings = (
        db.query(Holding)
        .join(Portfolio, Holding.portfolio_id == Portfolio.id)
        .filter(Portfolio.user_id == user_id)
        .all()
    )
    return {
        "holdings_count": len(holdings),
        "tickers": [h.ticker for h in holdings[:20]],
        "holdings": [
            {
                "name": resolve_position_name(
                    {"name": h.name, "ticker": h.ticker, "isin": h.isin}, db
                ),
                "ticker": h.ticker,
            }
            for h in holdings[:20]
        ],
    }


def _current_regime(db: Session) -> str:
    """Return the current market regime label or 'unknown'."""
    try:
        from app.lab.regime.macro_snapshot import get_or_refresh_regime
        regime = get_or_refresh_regime(db)
        return regime.get("label", "unknown") if isinstance(regime, dict) else "unknown"
    except Exception:
        return "unknown"


def _user_risk_profile(db: Session, user_id: str) -> str:
    user = db.get(User, user_id)
    return getattr(user, "risk_profile", "moderate") if user else "moderate"


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def build_context(db: Session, ticker: str, user_id: str) -> dict[str, Any]:
    """Gather all data dimensions into one dict for the LLM prompt."""
    q = quote(db, ticker)
    return {
        "ticker": ticker,
        "quote": q,
        "price_history": _price_history(db, ticker),
        "fundamentals": _latest_fundamentals(db, ticker),
        "recent_news": _recent_news(db, ticker),
        "recommendations": _existing_recommendations(db, ticker),
        "portfolio_context": _portfolio_context(db, user_id),
        "regime": _current_regime(db),
        "risk_profile": _user_risk_profile(db, user_id),
    }


# ---------------------------------------------------------------------------
# Report cache layer
# ---------------------------------------------------------------------------

def get_cached_report(db: Session, ticker: str, user_id: str) -> StockResearchReport | None:
    """Return a non-expired cached report or None."""
    row = (
        db.query(StockResearchReport)
        .filter(StockResearchReport.ticker == ticker, StockResearchReport.user_id == user_id)
        .order_by(StockResearchReport.generated_at.desc())
        .first()
    )
    if row and row.expires_at > datetime.now(UTC):
        return row
    return None


def save_report(db: Session, ticker: str, user_id: str, report_text: str,
                summary: str, context: dict[str, Any]) -> StockResearchReport:
    safe_report_text = ascii_safe(report_text)
    safe_summary = ascii_safe(summary)
    now = datetime.now(UTC)
    report = StockResearchReport(
        ticker=ticker,
        user_id=user_id,
        report_json=safe_report_text,
        executive_summary=safe_summary,
        data_snapshot_json=json.dumps(context, default=str),
        generated_at=now,
        expires_at=now + timedelta(hours=REPORT_TTL_HOURS),
    )
    try:
        db.add(report)
        db.commit()
        db.refresh(report)
    except Exception:
        db.rollback()
        raise
    return report


# ---------------------------------------------------------------------------
# LLM prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior equity research analyst.  Given the data below, produce
a concise but thorough stock research report.  The report must include:

1. **Executive Summary** — 2-3 sentence TL;DR.
2. **Price Action** — recent trend, support/resistance, volume.
3. **Technical View** — RSI, trend direction, momentum.
4. **Fundamental View** — valuation, profitability, balance sheet quality.
5. **Sentiment** — recent news and market sentiment.
6. **Risk Factors** — key risks specific to this stock.
7. **Conclusion & Recommendation** — actionable verdict (BUY / HOLD / SELL) with confidence (0-1).

Adapt tone to the user's risk profile (conservative → cautious, aggressive → opportunity-focused).
Include brief explanations for technical terms.
Output in Markdown.
"""


def _build_user_message(context: dict[str, Any]) -> str:
    parts = [f"## {context['ticker']} Research Context\n"]
    q = context.get("quote", {})
    if q:
        parts.append(f"**Current Price:** {q.get('price', 'N/A')} | Source: {q.get('source', 'N/A')}")
    parts.append(f"\n**Risk Profile:** {context.get('risk_profile', 'moderate')}")
    parts.append(f"**Market Regime:** {context.get('regime', 'unknown')}")

    # Price history summary
    history = context.get("price_history", [])
    if history:
        closes = [h["close"] for h in history]
        parts.append(f"\n**Price Range ({len(history)} days):** {min(closes):.2f} – {max(closes):.2f}")

    # Fundamentals
    fund = context.get("fundamentals")
    if fund:
        parts.append(f"\n**Fundamentals:** {json.dumps(fund, default=str)[:2000]}")

    # News
    news = context.get("recent_news", [])
    if news:
        parts.append("\n**Recent News:**")
        for n in news[:5]:
            parts.append(f"- [{n.get('sentiment_label', 'neutral')}] {n['title']} ({n['source']})")

    # Existing recommendations
    recs = context.get("recommendations", [])
    if recs:
        parts.append("\n**Prior Recommendations:**")
        for r in recs:
            parts.append(f"- {r['verdict']} ({r['confidence']:.0%}) — {r['horizon']}")

    # Portfolio context
    pc = context.get("portfolio_context", {})
    if pc.get("tickers"):
        in_portfolio = context["ticker"] in pc["tickers"]
        parts.append(f"\n**In Portfolio:** {'Yes' if in_portfolio else 'No'} ({pc['holdings_count']} total holdings)")

    parts.append("\n---\nGenerate the research report now.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def generate_report(db: Session, ticker: str, user_id: str, *,
                          force_refresh: bool = False) -> dict[str, Any]:
    """Generate or return a cached research report for *ticker*.

    Returns dict with keys: report, summary, context, cached, generated_at.
    """
    if not force_refresh:
        cached = get_cached_report(db, ticker, user_id)
        if cached:
            return {
                "report": cached.report_json,
                "summary": cached.executive_summary,
                "context": json.loads(cached.data_snapshot_json),
                "cached": True,
                "generated_at": cached.generated_at.isoformat(),
            }

    context = build_context(db, ticker, user_id)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(context)},
    ]

    from app.foundation.llm.router import call as llm_call
    try:
        completion = await llm_call(db, "batch_research", messages, timeout_s=120.0, user_id=user_id)
    except Exception as exc:
        logger.warning("LLM call failed for %s: %s", ticker, exc)
        return {
            "report": "",
            "summary": "Report generation unavailable.",
            "context": context,
            "cached": False,
            "generated_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
        }
    report_text = completion.content

    # Extract summary (first paragraph)
    summary = report_text.split("\n\n")[0] if report_text else ""
    try:
        report = save_report(db, ticker, user_id, report_text, summary, context)
    except Exception as exc:
        logger.warning("Report persist failed: %s", exc)
        db.rollback()
        return {
            "report": report_text,
            "summary": summary,
            "context": context,
            "cached": False,
            "generated_at": datetime.now(UTC).isoformat(),
            "persist_error": True,
        }

    return {
        "report": report_text,
        "summary": summary,
        "context": context,
        "cached": False,
        "generated_at": report.generated_at.isoformat(),
    }


async def generate_multi_horizon_verdicts(db: Session, ticker: str, user_id: str) -> dict[str, Any]:
    """Ask the LLM for multi-horizon BUY/HOLD/SELL verdicts and persist them."""
    # Return recent verdicts if they exist (avoid unbounded growth + redundant LLM calls)
    recent = (
        db.query(MultiHorizonVerdict)
        .filter(
            MultiHorizonVerdict.ticker == ticker,
            MultiHorizonVerdict.user_id == user_id,
            MultiHorizonVerdict.generated_at >= datetime.now(UTC) - timedelta(hours=REPORT_TTL_HOURS),
        )
        .order_by(MultiHorizonVerdict.generated_at.desc())
        .first()
    )
    if recent:
        horizons = json.loads(recent.horizons_json)
        return {"ticker": ticker, "verdicts": horizons, "generated_at": recent.generated_at.isoformat(), "cached": True}

    context = build_context(db, ticker, user_id)
    horizon_prompt = (
        f"You are a quantitative analyst.  Given the research context for {ticker}, "
        "produce a JSON array of verdicts for these horizons:\n"
        '1m, 1-6m, 6m-1y, 1-3y, 3-5y, 5y+\n'
        'Each item: {"horizon": "...", "verdict": "BUY|HOLD|SELL", "confidence": 0.0-1.0, "source": "llm"}\n'
        "Return ONLY the JSON array, no commentary."
    )
    messages = [
        {"role": "system", "content": "You are a quantitative analyst. Return only valid JSON."},
        {"role": "user", "content": horizon_prompt + "\n\nContext:\n" + _build_user_message(context)},
    ]

    from app.foundation.llm.router import call as llm_call
    completion = await llm_call(db, "batch_research", messages, timeout_s=60.0, user_id=user_id)

    # Try to parse JSON from response
    raw = completion.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        horizons = json.loads(raw)
    except json.JSONDecodeError:
        horizons = [{"horizon": "unknown", "verdict": "HOLD", "confidence": 0.5, "source": "llm"}]

    # Persist
    verdict = MultiHorizonVerdict(
        ticker=ticker,
        user_id=user_id,
        generated_at=datetime.now(UTC),
        horizons_json=json.dumps(horizons),
    )
    db.add(verdict)
    try:
        db.commit()
        db.refresh(verdict)
    except Exception:
        db.rollback()
        raise

    return {"ticker": ticker, "verdicts": horizons, "generated_at": verdict.generated_at.isoformat()}
