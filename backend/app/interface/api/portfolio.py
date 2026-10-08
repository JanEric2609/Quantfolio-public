"""Portfolio API — single owner of the ``/api/portfolio`` route namespace.

The advisor sub-namespace (``/api/portfolio/advisor/*``) lives in
``app.interface.api.portfolio_advisor`` as a separate router: its handler volume exceeds
the consolidation size budget and FastAPI's include semantics would prepend the
parent router's ``portfolio`` tag to its ``portfolio-advisor`` tag, drifting the
OpenAPI snapshot. See docs/archive/audits Wave 6 evidence for the tag-semantics proof.
"""

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import ASSET_TYPES, ActivityLedgerEntry, Benchmark, ConnectedAccount, Holding, PortfolioSnapshot, TargetAllocation, TransactionLog, WatchlistItem, User
from app.foundation.schemas import HoldingIn, HoldingOut, TransactionIn, TransactionOut, TransactionUpdate, WatchlistIn, WatchlistAlertIn, WatchlistOut
from app.foundation.schemas.common import TargetAllocationItem
from app.foundation.auth import current_user
from app.foundation.portfolio_service import allocation_by_asset_type, dkb_availability, main_portfolio, portfolio_performance, wealth_summary
from app.foundation.tax import german_tax_hints

logger = logging.getLogger(__name__)


def _safe_float(value, default: float = 0.0) -> float:
    """Cast numeric ORM fields to float, defaulting on None or conversion failure.

    Used by endpoints that must remain resilient against partially-populated rows
    (e.g. snapshots with NULL numeric columns) so that a single bad row never
    crashes the whole response.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _serialize_account(account: ConnectedAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "source": account.source,
        "name": account.name,
        "institution": account.institution,
        "type": account.account_type,
        "iban": account.iban,
        "balance": _safe_float(account.balance),
        "currency": account.currency,
        "last_synced": account.last_synced.isoformat() if account.last_synced else None,
    }


def _serialize_activity(entry: ActivityLedgerEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "connected_account_id": entry.connected_account_id,
        "source": entry.source,
        "activity_type": entry.activity_type,
        "date": entry.date.isoformat(),
        "amount": _safe_float(entry.amount),
        "currency": entry.currency,
        "description": entry.description,
        "isin": entry.isin,
        "symbol": entry.symbol,
        "quantity": _safe_float(entry.quantity) if entry.quantity is not None else None,
        "price": _safe_float(entry.price) if entry.price is not None else None,
        "review_state": entry.review_state,
    }


router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("/holdings", response_model=list[HoldingOut])
def list_holdings(
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[HoldingOut]:
    portfolio = main_portfolio(db, user.id)
    holdings = (
        db.query(Holding)
        .filter(Holding.portfolio_id == portfolio.id)
        .order_by(Holding.name.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [HoldingOut.model_validate(h) for h in holdings]


@router.post("/holdings", response_model=HoldingOut)
def create_holding(payload: HoldingIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> HoldingOut:
    portfolio = main_portfolio(db, user.id)
    holding = Holding(
        portfolio_id=portfolio.id,
        **payload.model_dump(),
        dkb_available=dkb_availability(payload.isin),
    )
    db.add(holding)
    db.commit()
    db.refresh(holding)
    return HoldingOut.model_validate(holding)


@router.put("/holdings/{holding_id}", response_model=HoldingOut)
def update_holding(
    holding_id: str,
    payload: HoldingIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HoldingOut:
    portfolio = main_portfolio(db, user.id)
    holding = db.get(Holding, holding_id)
    if holding is None or holding.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="Holding not found")
    for key, value in payload.model_dump().items():
        setattr(holding, key, value)
    holding.dkb_available = dkb_availability(payload.isin)
    db.commit()
    db.refresh(holding)
    return HoldingOut.model_validate(holding)


@router.delete("/holdings/{holding_id}")
def delete_holding(holding_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    portfolio = main_portfolio(db, user.id)
    holding = db.get(Holding, holding_id)
    if holding is None or holding.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="Holding not found")
    db.delete(holding)
    db.commit()
    return {"message": "Holding deleted"}


@router.get("/performance")
def performance(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    portfolio = main_portfolio(db, user.id)
    holdings = db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all()
    return portfolio_performance(holdings)


@router.get("/wealth")
def wealth(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return wealth_summary(db, user.id)


@router.get("/snapshots")
def snapshots(days: int = 30, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Return the user's recent daily wealth snapshots for portfolio charts."""
    try:
        wealth_summary(db, user.id)  # side-effect: ensures today's snapshot exists
        bounded_days = max(1, min(days, 3650))
        start_date = date.today() - timedelta(days=bounded_days - 1)
        rows = (
            db.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.user_id == user.id, PortfolioSnapshot.date >= start_date)
            .order_by(PortfolioSnapshot.date.desc(), PortfolioSnapshot.created_at.desc())
            .all()
        )
        by_date = {}
        for row in rows:
            by_date.setdefault(row.date, row)
        result = []
        for row in sorted(by_date.values(), key=lambda item: item.date):
            null_fields = [f for f in ("total_value", "cash_value", "security_value") if getattr(row, f, None) is None]
            if null_fields:
                logger.warning("snapshot %s/%s has NULL fields: %s", user.id, row.date, null_fields)
            result.append({
                "date": row.date.isoformat(),
                "total_value": _safe_float(row.total_value),
                "cash_value": _safe_float(row.cash_value),
                "security_value": _safe_float(row.security_value),
                "currency": row.currency,
            })
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("snapshots endpoint failed for user %s: %s", getattr(user, "id", "?"), exc)
        return []


@router.get("/accounts")
def connected_accounts(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    wealth_summary(db, user.id)
    rows = (
        db.query(ConnectedAccount)
        .filter(ConnectedAccount.user_id == user.id)
        .order_by(ConnectedAccount.source.asc(), ConnectedAccount.name.asc())
        .all()
    )
    return [_serialize_account(row) for row in rows]


@router.get("/activity")
def activity_ledger(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    try:
        wealth_summary(db, user.id)  # side-effect: ensures today's snapshot exists
        rows = (
            db.query(ActivityLedgerEntry)
            .filter(ActivityLedgerEntry.user_id == user.id)
            .order_by(ActivityLedgerEntry.date.desc(), ActivityLedgerEntry.created_at.desc())
            .limit(200)
            .all()
        )
        return [_serialize_activity(row) for row in rows]
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("activity endpoint failed for user %s: %s", getattr(user, "id", "?"), exc)
        return []


@router.get("/allocation")
def allocation(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    portfolio = main_portfolio(db, user.id)
    holdings = db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all()
    return {"by_asset_type": allocation_by_asset_type(holdings), "by_geography": {}, "by_sector": {}}


@router.get("/watchlist", response_model=list[WatchlistOut])
def list_watchlist(
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id)
        .order_by(WatchlistItem.ticker.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )


@router.post("/watchlist", response_model=WatchlistOut)
def add_watchlist(payload: WatchlistIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = WatchlistItem(user_id=user.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/watchlist/{item_id}/alert", response_model=WatchlistOut)
def set_watchlist_alert(
    item_id: str,
    payload: WatchlistAlertIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Set or clear the buy-alert target price on a watchlist item."""
    item = db.get(WatchlistItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    item.target_price = payload.target_price
    db.commit()
    db.refresh(item)
    return item


@router.delete("/watchlist/{item_id}")
def delete_watchlist(item_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, str]:
    item = db.get(WatchlistItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    db.delete(item)
    db.commit()
    return {"message": "Watchlist item deleted"}


@router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[TransactionOut]:
    portfolio = main_portfolio(db, user.id)
    holding_ids = [row.id for row in db.query(Holding.id).filter(Holding.portfolio_id == portfolio.id).all()]
    if not holding_ids:
        return []
    rows = db.query(TransactionLog).filter(TransactionLog.holding_id.in_(holding_ids)).all()
    return [TransactionOut.model_validate(r) for r in rows]


@router.post("/transactions", response_model=TransactionOut)
def create_transaction(payload: TransactionIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> TransactionOut:
    portfolio = main_portfolio(db, user.id)
    holding = db.get(Holding, payload.holding_id)
    if holding is None or holding.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="Holding not found")
    row = TransactionLog(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return TransactionOut.model_validate(row)


@router.put("/transactions/{transaction_id}", response_model=TransactionOut)
def update_transaction(
    transaction_id: str,
    payload: TransactionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransactionOut:
    portfolio = main_portfolio(db, user.id)
    row = db.get(TransactionLog, transaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    holding = db.get(Holding, row.holding_id)
    if holding is None or holding.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="Transaction not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return TransactionOut.model_validate(row)


@router.delete("/transactions/{transaction_id}")
def delete_transaction(transaction_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, str]:
    portfolio = main_portfolio(db, user.id)
    row = db.get(TransactionLog, transaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    holding = db.get(Holding, row.holding_id)
    if holding is None or holding.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="Transaction not found")
    db.delete(row)
    db.commit()
    return {"message": "Transaction deleted"}


@router.get("/benchmarks", response_model=None)
def benchmarks(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> list[Benchmark] | list[dict[str, str]]:
    rows = db.query(Benchmark).all()
    if rows:
        return rows
    return [
        {"ticker": "SXR8.DE", "name": "S&P 500 UCITS proxy"},
        {"ticker": "EXSA.DE", "name": "European equity benchmark proxy"},
        {"ticker": "VWCE.DE", "name": "FTSE All-World UCITS proxy"},
        {"ticker": "EUNA.DE", "name": "European bond benchmark proxy"},
    ]


@router.get("/tax/hints")
def tax_hints(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    return german_tax_hints(db, user.id)


# --- Target allocation (merged from app/api/target_allocation.py, Wave 6 W6.1;
# URLs unchanged: /api/portfolio/target-allocation[/drift]) ---

# Map holding-level asset types to target-level taxonomy
HOLDING_TYPE_TO_TARGET: dict[str, str | None] = {
    "stock": "equities",
    "etf": "equities",
    "bond": "bonds",
    "bond_etf": "bonds",
    "fund": "equities",
    "cash": "cash",
    "other": None,
}


def _serialize_target_allocation(row: TargetAllocation) -> dict[str, Any]:
    return {
        "id": row.id,
        "asset_type": row.asset_type,
        "target_pct": row.target_pct,
        "tolerance_pct": row.tolerance_pct,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/target-allocation")
def get_target_allocation(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, list[dict[str, Any]]]:
    rows = (
        db.query(TargetAllocation)
        .filter(TargetAllocation.user_id == user.id)
        .order_by(TargetAllocation.asset_type)
        .all()
    )
    return {"allocations": [_serialize_target_allocation(r) for r in rows]}


@router.put("/target-allocation")
def set_target_allocation(
    body: list[TargetAllocationItem],
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, list[dict[str, Any]]]:
    total = sum(item.target_pct for item in body)
    if abs(total - 100.0) > 0.01:
        raise HTTPException(
            status_code=400,
            detail=f"Target allocation percentages must sum to 100.0 (got {total:.2f})",
        )
    db.query(TargetAllocation).filter(TargetAllocation.user_id == user.id).delete()
    rows: list[TargetAllocation] = []
    for item in body:
        rows.append(
            TargetAllocation(
                user_id=user.id,
                asset_type=item.asset_type,
                target_pct=item.target_pct,
                tolerance_pct=item.tolerance_pct,
            )
        )
    db.add_all(rows)
    db.commit()
    for r in rows:
        db.refresh(r)
    return {"allocations": [_serialize_target_allocation(r) for r in rows]}


@router.get("/target-allocation/drift")
def get_drift(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Return current allocation mapped to target taxonomy, targets, and drift.

    The ``sleeves`` section reports the monthly plan's sleeve targets
    (core/tilt/satellite from ``decision.monthly_plan``) — the single place
    that defines targets (ADR 0019 §1) — next to the hand-set asset-taxonomy
    figures above it, so the two can be checked against each other.
    """
    portfolio = main_portfolio(db, user.id)
    holdings = db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all()
    raw = allocation_by_asset_type(holdings)

    # Map holding-level asset types to target-level taxonomy
    current: dict[str, float] = {}
    for holding_type, pct in raw.items():
        target_type = HOLDING_TYPE_TO_TARGET.get(holding_type)
        if target_type:
            current[target_type] = current.get(target_type, 0.0) + pct

    # Fill in missing asset types with 0
    for at in ASSET_TYPES:
        current.setdefault(at, 0.0)

    rows = (
        db.query(TargetAllocation)
        .filter(TargetAllocation.user_id == user.id)
        .all()
    )
    target_map: dict[str, Any] = {}
    for r in rows:
        target_map[r.asset_type] = {"target_pct": r.target_pct, "tolerance_pct": r.tolerance_pct}

    drift: dict[str, float] = {}
    for at in ASSET_TYPES:
        curr = current.get(at, 0.0)
        tgt = target_map.get(at, {})
        drift[at] = round(curr - tgt.get("target_pct", 0.0), 2)

    from app.decision.monthly_plan import sleeve_plan

    sleeves = sleeve_plan(db, user.id)

    return {
        "current": current,
        "targets": [_serialize_target_allocation(r) for r in rows],
        "drift": drift,
        "sleeves": {
            "targets": sleeves["targets"],
            "unlocked": sleeves["unlocked"],
            "labels": sleeves["labels"],
            "band_pp": sleeves["band_pp"],
        },
    }
