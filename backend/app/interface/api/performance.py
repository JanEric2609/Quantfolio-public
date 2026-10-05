"""Performance ledger API endpoints (Phase 3)."""

import logging
from datetime import datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case
from sqlalchemy.orm import Session
from app.foundation.core.db import get_db
from app.foundation.auth import current_user
from app.interface.envelope import three_artifacts
from app.lab.performance_ledger import (
    create_composite,
    list_composites,
    get_composite,
    get_composite_members,
    write_ledger_snapshot,
    time_weighted_return,
    composite_time_weighted_return,
    money_weighted_return_irr,
    ensure_default_composite,
)
from app.lab.performance_ledger.ex_post_risk import calculate_ex_post_risk
from app.decision.verification.benchmark import benchmark_comparison
from app.foundation.settings import get_risk_free_rate
from app.foundation.models.entities import (
    ActivityLedgerEntry,
    CompositeMembership,
    PerformanceLedgerEntry,
    PortfolioSnapshot,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/performance", tags=["performance"])


class CompositeRequest(BaseModel):
    """Request to create a composite."""
    name: str
    portfolio_ids: list[str]


class LedgerSnapshotRequest(BaseModel):
    """Request to write a ledger snapshot.

    TWR, MWR, and ex-post risk are all computed server-side.
    Client-supplied values are ignored.
    """
    composite_id: str
    as_of: datetime
    dispersion: float | None = None


@router.post("/composites")
def create_composite_endpoint(
    request: CompositeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Create a composite portfolio."""
    composite = create_composite(
        db, user_id=user.id, name=request.name, portfolio_ids=request.portfolio_ids
    )
    db.commit()

    return three_artifacts(
        research={
            "id": composite.id,
            "name": composite.name,
            "portfolio_ids": request.portfolio_ids,
            "created_at": composite.created_at.isoformat(),
        }
    )


@router.get("/composites")
def list_composites_endpoint(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """List user's composites."""
    # Ensure default composite exists (idempotent)
    ensure_default_composite(db, user_id=user.id)
    db.commit()

    composites = list_composites(db, user_id=user.id)

    import json

    return three_artifacts(
        research={
            "composites": [
                {
                    "id": c.id,
                    "name": c.name,
                    "definition": json.loads(c.definition_json) if c.definition_json else {},
                    "created_at": c.created_at.isoformat(),
                }
                for c in composites
            ]
        }
    )


@router.post("/ledger/snapshot")
def write_ledger_snapshot_endpoint(
    request: LedgerSnapshotRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Write a performance ledger snapshot.

    TWR and MWR are computed server-side from portfolio snapshots
    and cashflows. Client-supplied values are ignored.
    """
    composite = get_composite(db, request.composite_id)
    if not composite or composite.user_id != user.id:
        raise HTTPException(status_code=404, detail="Composite not found")

    as_of_date = request.as_of.date()

    portfolio_ids = get_composite_members(db, request.composite_id, as_of=as_of_date)
    if not portfolio_ids:
        raise HTTPException(status_code=400, detail="Composite has no portfolios")

    # External cashflows for TWR/MWR are deposits/withdrawals that move money
    # into or out of the whole portfolio — NOT security trades. A buy converts
    # cash into equity with net wealth unchanged, so feeding security trades in
    # here (the previous behaviour) booked every €X purchase as a €X external
    # contribution against total-wealth snapshots, destroying the return series.
    # The activity ledger already separates external cashflows (activity_type
    # "cashflow") from internal security reallocations (activity_type "buy"/"sell").
    external_flows = (
        db.query(ActivityLedgerEntry)
        .filter(ActivityLedgerEntry.user_id == user.id)
        .filter(ActivityLedgerEntry.activity_type == "cashflow")
        .filter(ActivityLedgerEntry.date <= as_of_date)
        .order_by(ActivityLedgerEntry.date)
        .all()
    )
    # Ledger sign convention matches TWR/MWR: positive = deposit, negative = withdrawal.
    cashflows = [{"date": entry.date, "amount": float(entry.amount)} for entry in external_flows]

    # PortfolioSnapshot has a unique constraint on (user_id, date, source) — a
    # user can have both a `dkb_mirror` row and a `computed` row on the same
    # date with different total_value. Ordering by date alone leaves the
    # tie-break on same-date rows arbitrary, producing a spurious return
    # between two differently-scaled values. Prefer dkb_mirror on ties, then
    # dedupe to one row per date (see divergence.py for the same pattern).
    source_priority = case(
        (PortfolioSnapshot.source == "dkb_mirror", 0),
        else_=1,
    )
    raw_snapshots = (
        db.query(PortfolioSnapshot)
        .filter_by(user_id=user.id)
        .filter(PortfolioSnapshot.date <= as_of_date)
        .order_by(PortfolioSnapshot.date, source_priority.asc())
        .all()
    )
    seen_dates: set = set()
    snapshots = []
    for snap in raw_snapshots:
        if snap.date in seen_dates:
            continue
        seen_dates.add(snap.date)
        snapshots.append(snap)

    if len(snapshots) < 2:
        raise HTTPException(
            status_code=400,
            detail="Insufficient snapshot data for TWR/MWR computation (need at least 2 daily snapshots)",
        )

    snapshot_dicts = [
        {"date": s.date, "value": float(s.total_value)}
        for s in snapshots
    ]

    period_start = snapshot_dicts[0]["date"]

    # Composite TWR honours effective-dated membership: a portfolio joining or
    # leaving the composite mid-period is a *reweighting*, not a cashflow, so its
    # market value is not booked as a composite gain/loss (P5). Built from
    # per-portfolio member snapshots when available; otherwise fall back to the
    # user-level total series (single implicit composite = the whole account).
    membership_rows = (
        db.query(CompositeMembership)
        .filter(CompositeMembership.composite_id == request.composite_id)
        .all()
    )
    memberships = [
        {"portfolio_id": r.portfolio_id, "valid_from": r.valid_from, "valid_to": r.valid_to}
        for r in membership_rows
    ]
    member_ids = {m["portfolio_id"] for m in memberships}
    member_snapshots: dict[str, list[dict]] = {}
    if member_ids:
        for s in (
            db.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.portfolio_id.in_(member_ids))
            .filter(PortfolioSnapshot.date <= as_of_date)
            .order_by(PortfolioSnapshot.date)
            .all()
        ):
            assert s.portfolio_id is not None
            member_snapshots.setdefault(s.portfolio_id, []).append(
                {"date": s.date, "value": float(s.total_value)}
            )

    if member_snapshots:
        comp_period_start = min(
            snap["date"] for snaps in member_snapshots.values() for snap in snaps
        )
        twr = composite_time_weighted_return(
            member_snapshots,
            memberships,
            comp_period_start,
            as_of_date,
            cashflows=cashflows,
        )
    else:
        twr = time_weighted_return(
            snapshot_dicts,
            cashflows=cashflows,
            period_start=period_start,
            period_end=as_of_date,
        )

    opening_value = snapshot_dicts[0]["value"]
    closing_value = snapshot_dicts[-1]["value"]
    mwr = money_weighted_return_irr(
        opening_value=opening_value,
        closing_value=closing_value,
        cashflows=cashflows,
        period_start=period_start,
        period_end=as_of_date,
    )

    logger.info(
        "Computed TWR=%.4f MWR=%.4f for composite=%s (snapshots=%d, cashflows=%d)",
        twr, mwr, request.composite_id, len(snapshot_dicts), len(cashflows),
    )

    daily_returns = [
        (snapshot_dicts[i]["value"] - snapshot_dicts[i - 1]["value"]) / snapshot_dicts[i - 1]["value"]
        for i in range(1, len(snapshot_dicts))
        if snapshot_dicts[i - 1]["value"] > 0
    ]
    ex_post_risk_metrics = (
        calculate_ex_post_risk(daily_returns, risk_free_rate=get_risk_free_rate(db))
        if len(daily_returns) >= 2
        else None
    )

    entry = write_ledger_snapshot(
        db,
        composite_id=request.composite_id,
        as_of=request.as_of,
        twr=twr,
        mwr=mwr,
        dispersion=request.dispersion,
        ex_post_risk=ex_post_risk_metrics,
        snapshot_meta={
            "user_id": user.id,
            "snapshots_used": len(snapshot_dicts),
            "cashflows_used": len(cashflows),
            "computation": "server_side",
        },
    )
    db.commit()

    import json

    return three_artifacts(
        research={
            "id": entry.id,
            "composite_id": entry.composite_id,
            "as_of": entry.as_of.isoformat(),
            "twr": entry.twr,
            "mwr": entry.mwr,
            "dispersion": entry.dispersion,
            "ex_post_risk": json.loads(entry.ex_post_risk_json) if entry.ex_post_risk_json else {},
        },
        ledger_ref=str(entry.id),
    )


@router.get("/ledger")
def get_ledger_entries(
    composite_id: str,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get performance ledger entries for a composite."""
    composite = get_composite(db, composite_id)
    if not composite or composite.user_id != user.id:
        raise HTTPException(status_code=404, detail="Composite not found")

    entries = (
        db.query(PerformanceLedgerEntry)
        .filter_by(composite_id=composite_id)
        .order_by(PerformanceLedgerEntry.as_of.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    import json

    return three_artifacts(
        research={
            "composite_id": composite_id,
            "entries": [
                {
                    "id": e.id,
                    "as_of": e.as_of.isoformat(),
                    "twr": e.twr,
                    "mwr": e.mwr,
                    "dispersion": e.dispersion,
                    "ex_post_risk": json.loads(e.ex_post_risk_json) if e.ex_post_risk_json else {},
                    "snapshot_meta": json.loads(e.snapshot_meta_json) if e.snapshot_meta_json else {},
                }
                for e in entries
            ],
            "count": len(entries),
        }
    )


@router.get("/ledger/{composite_id}/summary")
def get_ledger_summary(
    composite_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get summary of latest ledger entry."""
    composite = get_composite(db, composite_id)
    if not composite or composite.user_id != user.id:
        raise HTTPException(status_code=404, detail="Composite not found")

    latest = (
        db.query(PerformanceLedgerEntry)
        .filter_by(composite_id=composite_id)
        .order_by(PerformanceLedgerEntry.as_of.desc())
        .first()
    )

    if not latest:
        raise HTTPException(status_code=404, detail="No ledger entries found")

    import json

    return three_artifacts(
        research={
            "id": latest.id,
            "composite_id": latest.composite_id,
            "as_of": latest.as_of.isoformat(),
            "twr": latest.twr,
            "mwr": latest.mwr,
            "dispersion": latest.dispersion,
            "ex_post_risk": json.loads(latest.ex_post_risk_json) if latest.ex_post_risk_json else {},
        },
        ledger_ref=str(latest.id),
    )


@router.get("/ledger/{composite_id}/benchmark")
def get_ledger_benchmark(
    composite_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Latest ledger TWR beside the passive core ETF's return over the same window.

    The window runs from the user's first portfolio snapshot (where the ledger's
    time-weighted return starts) to the latest entry's ``as_of``.
    """
    composite = get_composite(db, composite_id)
    if not composite or composite.user_id != user.id:
        raise HTTPException(status_code=404, detail="Composite not found")

    latest = (
        db.query(PerformanceLedgerEntry)
        .filter_by(composite_id=composite_id)
        .order_by(PerformanceLedgerEntry.as_of.desc())
        .first()
    )
    if not latest:
        raise HTTPException(status_code=404, detail="No ledger entries found")

    first_snapshot = (
        db.query(PortfolioSnapshot.date)
        .filter(PortfolioSnapshot.user_id == user.id)
        .order_by(PortfolioSnapshot.date.asc())
        .first()
    )
    if first_snapshot is None:
        raise HTTPException(status_code=404, detail="No portfolio snapshots found")

    return three_artifacts(
        research={
            "composite_id": composite_id,
            **benchmark_comparison(
                db,
                period_start=first_snapshot[0],
                period_end=latest.as_of.date(),
                portfolio_twr=float(latest.twr) if latest.twr is not None else None,
            ),
        }
    )
