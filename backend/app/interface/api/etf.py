"""ETF API endpoints."""


from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.etf_classification import classify_and_enrich
from app.foundation.etf_lookup import get_etf_composition, get_etf_profile, tracking_stats
from app.foundation.etf_overlap import pairwise_overlap, portfolio_overlap

router = APIRouter(prefix="/api/etf", tags=["etf"])


@router.get("/{ticker}/composition")
def get_etf_composition_endpoint(
    ticker: str,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get ETF composition data including holdings, sectors, and regions."""
    composition = get_etf_composition(ticker)
    if composition is None:
        raise HTTPException(
            status_code=404,
            detail=f"ETF composition data not found for ticker: {ticker}"
        )
    return composition.model_dump()


@router.get("/{ticker}/profile")
def get_etf_profile_endpoint(
    ticker: str,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get enriched ETF profile (TER, size, domicile, holdings, sectors, regions)."""
    profile = get_etf_profile(db, ticker)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"ETF profile not found for ticker: {ticker}"
        )
    return profile


@router.get("/tracking")
def get_tracking_stats(
    ticker: str = Query(..., description="ETF ticker"),
    index: str = Query(..., description="Benchmark index ticker"),
    years: int = Query(3, ge=1, le=10, description="Look-back years"),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Calculate tracking error, beta, and R² for an ETF against an index."""
    stats = tracking_stats(db, ticker, index, years=years)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"Tracking stats unavailable for {ticker} vs {index}"
        )
    return stats


@router.get("/overlap")
def get_pairwise_overlap(
    a: str = Query(..., description="First ETF ticker"),
    b: str = Query(..., description="Second ETF ticker"),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Compute holding overlap between two ETFs."""
    comp_a = get_etf_composition(a)
    comp_b = get_etf_composition(b)
    if comp_a is None or comp_b is None:
        raise HTTPException(
            status_code=404,
            detail="ETF composition data unavailable for one or both tickers"
        )
    holdings_a = [{"ticker": h.ticker, "weight": h.weight} for h in comp_a.holdings]
    holdings_b = [{"ticker": h.ticker, "weight": h.weight} for h in comp_b.holdings]
    return pairwise_overlap(holdings_a, holdings_b)


@router.get("/portfolio/overlap")
def get_portfolio_overlap(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Overlap matrix for all ETF holdings in the user's portfolio."""
    result = portfolio_overlap(db, user.id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No portfolio found or insufficient ETF holdings"
        )
    return result


@router.post("/classify")
def post_classify(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Trigger ETF classification and asset enrichment for the current user."""
    result = classify_and_enrich(db, user.id)
    return result
