import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.interface.api.safe_endpoint import safe_endpoint
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.decision.paper_portfolio import (
    compute_metrics,
    execute_trade,
    get_holdings,
    get_or_create_paper_portfolio,
    get_snapshots,
    get_summary,
    get_trades,
    passive_benchmark,
    reseed_manual_portfolio,
    reset_portfolio,
    snapshot_paper_portfolio,
)
from app.foundation.models.entities import PaperPortfolio, PaperPortfolioArchive

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper-portfolio", tags=["paper-portfolio"])


class TradeIn(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)
    price: float = Field(gt=0, description="Per unit, in EUR.")
    confidence: float | None = Field(None, ge=0, le=1)
    rationale: str | None = None


@router.get("/")
@safe_endpoint
def paper_portfolio_summary(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    portfolio, seeded_from_dkb = get_or_create_paper_portfolio(db, user.id)
    summary = get_summary(db, portfolio.id)
    summary["seeded_from_dkb"] = seeded_from_dkb
    return summary


@router.get("/holdings")
@safe_endpoint
def list_holdings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    return get_holdings(db, portfolio.id)


@router.get("/trades")
@safe_endpoint
def list_trades(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    trades = get_trades(db, portfolio.id)
    return [
        {
            "id": t.id,
            "ticker": t.ticker,
            "side": t.side,
            "quantity": float(t.quantity),
            "price": float(t.price),
            "value": float(t.value),
            "fee": float(t.fee or 0),
            "date": t.date.isoformat() if t.date else None,
            "confidence": t.confidence,
            "rationale": t.rationale,
        }
        for t in trades
    ]


@router.get("/snapshots")
@safe_endpoint
def list_snapshots(days: int = 365, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    snapshots = get_snapshots(db, portfolio.id, days=days)
    return [
        {
            "date": s.date.isoformat(),
            "total_value": float(s.total_value),
            "cash_balance": float(s.cash_balance),
            "securities_value": float(s.securities_value),
            "total_return_pct": float(s.total_return_pct),
        }
        for s in snapshots
    ]


@router.post("/snapshot")
@safe_endpoint
def trigger_snapshot(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    result = snapshot_paper_portfolio(db, portfolio.id)
    # compute_metrics runs after snapshot_paper_portfolio commits.
    # If it fails, the snapshot is already persisted; we re-raise so the client
    # knows metrics are missing, but we do not roll back the snapshot.
    try:
        compute_metrics(db, portfolio.id)
    except Exception as exc:
        logger.error("compute_metrics failed after snapshot for portfolio %s: %s", portfolio.id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Snapshot saved but metrics computation failed")
    return result


@router.post("/reseed")
@safe_endpoint
def reseed_paper_portfolio(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Re-seed the manual paper portfolio from the current synced positions (DKB, Scalable).

    Clears existing holdings and re-imports them, one per ISIN. Updates
    initial_cash from the current cash balances at every synced bank and broker.
    Returns 404 if the manual portfolio does not yet exist.
    """
    from app.foundation.models.entities import PaperPortfolio as _PP
    portfolio = (
        db.query(_PP)
        .filter(_PP.user_id == user.id, _PP.mandate == "manual")
        .first()
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="Manual portfolio not found — load the page first to create it")

    holdings_count, seeded_from_dkb = reseed_manual_portfolio(db, portfolio.id, user.id)
    logger.info("Reseeded manual portfolio %s — now has %d holdings", portfolio.id, holdings_count)
    return {
        "id": portfolio.id,
        "holdings_count": holdings_count,
        "seeded_from_dkb": seeded_from_dkb,
    }


@router.post("/trade")
@safe_endpoint
def add_trade(payload: TradeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    try:
        return execute_trade(
            db,
            portfolio.id,
            ticker=payload.ticker,
            side=payload.side,
            quantity=payload.quantity,
            price=payload.price,
            confidence=payload.confidence,
            rationale=payload.rationale,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _owned(db: Session, portfolio_id: str, user: User) -> PaperPortfolio:
    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None or portfolio.user_id != user.id:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


class ResetIn(BaseModel):
    confirm: bool = Field(description="Must be true: the current run is archived and a new one starts today.")
    reason: str = Field(default="reset", max_length=200)


@router.post("/{portfolio_id}/reset")
@safe_endpoint
def reset_paper_portfolio(
    portfolio_id: str, payload: ResetIn, user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Archive the current run and start again today from the synced book at market value."""
    _owned(db, portfolio_id, user)
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="Set confirm to true to reset this portfolio.")
    return reset_portfolio(db, portfolio_id, reason=payload.reason)


@router.get("/{portfolio_id}/archives")
@safe_endpoint
def list_archives(portfolio_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Earlier runs of this portfolio, newest first (counts only; the payload stays in the table)."""
    import json

    _owned(db, portfolio_id, user)
    rows = (
        db.query(PaperPortfolioArchive)
        .filter(PaperPortfolioArchive.portfolio_id == portfolio_id)
        .order_by(PaperPortfolioArchive.archived_at.desc())
        .all()
    )
    out = []
    for r in rows:
        payload = json.loads(r.payload_json or "{}")
        snaps = payload.get("snapshots") or []
        out.append({
            "id": r.id,
            "archived_at": r.archived_at.isoformat() if r.archived_at else None,
            "inception_at": r.inception_at.isoformat() if r.inception_at else None,
            "reason": r.reason,
            "trades": len(payload.get("trades") or []),
            "snapshots": len(snaps),
            "last_total_return_pct": float(snaps[-1]["total_return_pct"]) if snaps else None,
        })
    return out


@router.get("/{portfolio_id}/performance")
@safe_endpoint
def paper_performance(portfolio_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Daily value of the run since inception next to the same start in MSCI World EUR."""
    portfolio = _owned(db, portfolio_id, user)
    summary = get_summary(db, portfolio_id)
    bench = passive_benchmark(db, portfolio, summary["baseline_value"], series=True)
    bench_series = bench.pop("series", {}) if bench.get("available") else {}
    snaps = get_snapshots(db, portfolio_id, days=3650)
    return {
        "summary": summary,
        "benchmark": bench,
        "series": [
            {
                "date": s.date.isoformat(),
                "value": float(s.total_value),
                "benchmark": bench_series.get(s.date.isoformat()),
            }
            for s in snaps
        ],
        "method": "No external flows after the seed, so value / starting value - 1 is the time-weighted return.",
    }
