"""Stock Research Hub — API endpoints.

Covers the research data flow: report generation, multi-horizon verdicts,
risk-profile updates, and the aggregated stock context endpoint.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.interface.api.safe_endpoint import safe_endpoint
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.schemas.common import OrmModel
from app.foundation.auth import current_user

router = APIRouter(prefix="/api/research", tags=["research"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StockReportResponse(OrmModel):
    report: str
    summary: str
    context: dict[str, Any] | None = None
    cached: bool
    generated_at: str


class VerdictResponse(OrmModel):
    ticker: str
    verdicts: list[dict[str, Any]]
    generated_at: str


class RiskProfileIn(BaseModel):
    risk_profile: str  # "conservative" | "moderate" | "aggressive"


class RiskProfileOut(OrmModel):
    risk_profile: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/stock/{ticker}/report", response_model=StockReportResponse)
@safe_endpoint
async def get_stock_report(
    ticker: str,
    force: bool = Query(False, description="Force regenerate even if cached"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get or generate an LLM research report for *ticker*."""
    from app.foundation.research import generate_report
    result = await generate_report(db, ticker.upper(), user.id, force_refresh=force)
    return result


@router.post("/stock/{ticker}/report", response_model=StockReportResponse)
@safe_endpoint
async def regenerate_stock_report(
    ticker: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Force-regenerate the research report (always fresh)."""
    from app.foundation.research import generate_report
    result = await generate_report(db, ticker.upper(), user.id, force_refresh=True)
    return result


@router.get("/stock/{ticker}/piotroski")
@safe_endpoint
async def get_piotroski_score(
    ticker: str,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Piotroski-inspired fundamental strength score (0–9) for *ticker*."""
    from app.foundation.piotroski import compute_piotroski
    return compute_piotroski(db, ticker.upper())


@router.get("/stock/{ticker}/sentiment")
@safe_endpoint
async def get_sentiment(
    ticker: str,
    days: int = Query(21, description="Look-back window in days"),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Aggregated news sentiment for *ticker* (BERT-enriched when available)."""
    from app.foundation.sentiment import aggregate_sentiment
    return aggregate_sentiment(db, ticker.upper(), days=days)


@router.get("/stock/{ticker}/verdicts", response_model=VerdictResponse)
@safe_endpoint
async def get_stock_verdicts(
    ticker: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get multi-horizon BUY/HOLD/SELL verdicts for *ticker*."""
    from app.foundation.research import generate_multi_horizon_verdicts
    result = await generate_multi_horizon_verdicts(db, ticker.upper(), user.id)
    return result


@router.get("/stock/{ticker}")
@safe_endpoint
async def get_stock_context(
    ticker: str,
    include: str = Query("report,chart,news", description="Comma-separated data to include"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Aggregated stock research context (chart data, TA, report, news)."""
    from app.foundation.research import get_cached_report
    from app.foundation.market import quote as get_quote

    ticker_upper = ticker.upper()

    # Block sentinel ticker used for portfolio-level reports
    from app.foundation.portfolio_analysis import PORTFOLIO_TICKER
    if ticker_upper == PORTFOLIO_TICKER:
        raise HTTPException(status_code=400, detail="Use /api/research/portfolio-report for portfolio-level reports")

    parts = include.split(",")
    result: dict[str, Any] = {"ticker": ticker_upper}

    # Always include quote
    try:
        result["quote"] = get_quote(db, ticker_upper)
    except Exception:
        result["quote"] = {}

    # Build full context if report requested
    if "report" in parts:
        cached = get_cached_report(db, ticker_upper, user.id)
        if cached:
            result["report"] = {
                "text": cached.report_json,
                "summary": cached.executive_summary,
                "generated_at": cached.generated_at.isoformat(),
                "cached": True,
            }
        else:
            result["report"] = None

    # Price history for chart
    if "chart" in parts:
        from app.foundation.research import _price_history
        result["price_history"] = _price_history(db, ticker_upper, days=365)

    # News
    if "news" in parts:
        from app.foundation.research import _recent_news
        result["news"] = _recent_news(db, ticker_upper)

    # Fundamentals
    if "fundamentals" in parts:
        from app.foundation.research import _latest_fundamentals
        result["fundamentals"] = _latest_fundamentals(db, ticker_upper)

    return result


# ---------------------------------------------------------------------------
# User risk profile
# ---------------------------------------------------------------------------

@router.get("/risk-profile", response_model=RiskProfileOut)
@safe_endpoint
def get_risk_profile(user: User = Depends(current_user)) -> RiskProfileOut:
    """Get the current user's risk profile."""
    return RiskProfileOut(risk_profile=user.risk_profile or "moderate")


@router.put("/risk-profile", response_model=RiskProfileOut)
@safe_endpoint
def update_risk_profile(
    payload: RiskProfileIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> RiskProfileOut:
    """Update the user's risk profile."""
    valid_profiles = {"conservative", "moderate", "aggressive"}
    if payload.risk_profile not in valid_profiles:
        raise HTTPException(status_code=400, detail=f"Invalid risk profile. Must be one of: {valid_profiles}")
    user.risk_profile = payload.risk_profile
    db.commit()
    db.refresh(user)
    return RiskProfileOut(risk_profile=user.risk_profile)


# ---------------------------------------------------------------------------
# Portfolio-level LLM analysis
# ---------------------------------------------------------------------------

class PortfolioReportResponse(OrmModel):
    report: str
    summary: str
    cached: bool
    generated_at: str
    error: str | None = None
    context: dict[str, Any] | None = None


@router.get("/portfolio-report", response_model=PortfolioReportResponse)
@safe_endpoint
async def get_portfolio_report(
    force: bool = Query(False, description="Force regenerate even if cached"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Get or generate a portfolio-level LLM analysis.

    Synthesizes all individual stock reports, allocation data, regime state,
    and risk profile into a cohesive portfolio view.  Cached for 7 days.
    """
    from app.foundation.portfolio_analysis import generate_portfolio_report
    result = await generate_portfolio_report(db, user.id, force=force)
    return PortfolioReportResponse(**result)


@router.post("/portfolio-report", response_model=PortfolioReportResponse)
@safe_endpoint
async def regenerate_portfolio_report(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Force-regenerate the portfolio-level LLM analysis."""
    from app.foundation.portfolio_analysis import generate_portfolio_report
    result = await generate_portfolio_report(db, user.id, force=True)
    return PortfolioReportResponse(**result)
