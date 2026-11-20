"""Graduation API: the capital ladder and the old graduation criteria.

``GET /ladder`` reports the capital ladder (ADR 0025): how much of the book
stock picks may hold, per candidate and stage. ``GET /status`` keeps its shape
with the old criteria marked legacy and a ``ladder`` field added. ``POST /recommendations`` is gated on a
graduated verdict; when unlocked it reuses the
existing recommendation engine and surfaces results into the Review Inbox.

No endpoint here executes trades. Graduated output is advisory only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import GraduationAssessment, User
from app.foundation.auth import current_user
from app.decision.graduation import GraduationResult, evaluate_graduation, ladder_state
from app.foundation.settings import get_public_settings
from app.interface.api.params import LimitQuery

router = APIRouter(prefix="/api/graduation", tags=["graduation"])


def _passive_core_block(db: Session) -> dict[str, Any]:
    """The ADR 0015 "silent period" default (rulings #13/#23): while no
    strategy clears the 5-condition gate, real-money guidance is a single
    global equity ETF rather than stock-level picks. Always present so the
    frontend can show it whenever ``graduated`` is false — the expected
    normal state at a ledger-backed n_trials floor of 500."""
    settings = get_public_settings(db)
    ticker = settings.get("passive_core_ticker", "EUNL.DE")
    return {
        "mode": settings.get("decision_loop_mode", "passive_core"),
        "ticker": ticker,
        "isin": settings.get("passive_core_isin", "IE00B4L5Y983"),
        "message": (
            f"Recommendations are paused pending statistical proof — hold {ticker}. "
            "See /evidence for gate status."
        ),
    }

# Minimum spacing between persisted snapshots when nothing material changed.
# Keeps ``/status`` effectively a read (the page is polled on every mount) while
# still trending progress over time. A changed verdict/progress always persists.
_SNAPSHOT_MIN_INTERVAL = timedelta(hours=12)


def _persist_if_due(db: Session, user_id: str, result: GraduationResult) -> None:
    """Write a snapshot only when it adds signal.

    Reading status must not balloon the table on every page view, and ``/history``
    should be a meaningful trend rather than view-triggered noise. So we persist
    only when there is no prior snapshot, the verdict or progress changed, or the
    last snapshot is older than ``_SNAPSHOT_MIN_INTERVAL``.
    """
    latest = (
        db.query(GraduationAssessment)
        .filter(GraduationAssessment.user_id == user_id)
        .order_by(GraduationAssessment.created_at.desc())
        .first()
    )
    if latest is not None:
        changed = (
            latest.graduated != result.graduated
            or abs((latest.overall_progress or 0.0) - result.overall_progress) >= 1e-9
        )
        last_at = latest.created_at
        if last_at is not None and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        recent = last_at is not None and last_at >= datetime.now(timezone.utc) - _SNAPSHOT_MIN_INTERVAL
        if not changed and recent:
            return

    row = GraduationAssessment(
        user_id=user_id,
        graduated=result.graduated,
        overall_progress=result.overall_progress,
        criteria_json=json.dumps([c.__dict__ for c in result.criteria], default=str),
        metrics_json=json.dumps(result.metrics, default=str),
    )
    db.add(row)
    db.commit()


@router.get("/status")
def status(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Current graduation verdict + per-criterion progress.

    Snapshots a trend point only when the verdict/progress changed or the last
    snapshot is stale (see ``_persist_if_due``), so polling this endpoint does
    not write a row on every page view.

    Returns:
        dict[str, Any]: Graduation assessment including per-criterion progress,
            overall progress, metrics, and recommendations unlock status.
    """
    from app.foundation.models.entities import AdvisorGraduationState

    result = evaluate_graduation(db, user.id)
    _persist_if_due(db, user.id, result)
    # ADR 0025: the capital ladder writes the switch (nightly); the old
    # criteria are display only and no longer flip it.
    state = (
        db.query(AdvisorGraduationState).filter(AdvisorGraduationState.user_id == user.id).one_or_none()
        or AdvisorGraduationState(user_id=user.id, graduated=False)
    )
    ladder = ladder_state(db, user.id)
    graduated = any(c["stage"] >= 2 for c in ladder["candidates"])
    payload = result.to_dict()
    payload["legacy_graduated"] = result.graduated
    payload["graduated"] = graduated
    payload["recommendations_unlocked"] = graduated
    payload["ladder"] = ladder
    payload["passive_core"] = _passive_core_block(db)
    payload["state"] = {
        "graduated": state.graduated,
        "since": state.since.isoformat() if state.since else None,
        "last_transition_at": (
            state.last_transition_at.isoformat() if state.last_transition_at else None
        ),
        "last_reason": state.last_reason,
        "strategy_id": state.strategy_id,
    }
    return payload


@router.get("/ladder")
def ladder(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """The capital ladder (ADR 0025): per candidate its stage, weight, e-values,
    tracking error, relative drawdown against its band, days of evidence, the
    next stage and what it needs, and the history for a chart; plus which
    candidate funds the satellite this month. Advisory only.
    """
    return ladder_state(db, user.id)


class FillPrice(BaseModel):
    price_eur: float = Field(gt=0, lt=1_000_000)


@router.get("/learning-sleeve")
def learning_sleeve(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    """The learning sleeve's purchases, the same amounts in the benchmark ETF, and the fills against paper prices.

    Detects a filled order from the synced Trading 212 holdings first. A learning expense the owner
    chose, not evidence about the picks. Nothing here places an order.
    """
    from app.decision.learning_ledger import detect_fills, results

    detect_fills(db, user.id)
    return results(db, user.id)


@router.put("/learning-sleeve/{purchase_id}/fill-price")
def learning_sleeve_fill_price(
    purchase_id: str, body: FillPrice, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    """The owner types the real fill price (EUR per unit) from the broker app."""
    from app.decision.learning_ledger import record_fill_price, results

    if record_fill_price(db, user.id, purchase_id, body.price_eur) is None:
        raise HTTPException(status_code=404, detail="Unknown purchase")
    return results(db, user.id)


@router.get("/transitions")
def transitions(
    limit: LimitQuery = 30,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    """Graduate/de-graduate flip history with reasons (audit trail, PR2 D2)."""
    from app.foundation.models.entities import AdvisorGraduationTransition

    rows = (
        db.query(AdvisorGraduationTransition)
        .filter(AdvisorGraduationTransition.user_id == user.id)
        .order_by(AdvisorGraduationTransition.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [
        {
            "id": r.id,
            "strategy_id": r.strategy_id,
            "from_graduated": r.from_graduated,
            "to_graduated": r.to_graduated,
            "reason": r.reason,
            "criteria": r.criteria_json or {},
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/history")
def history(
    limit: LimitQuery = 30,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    """
    Retrieve the user's graduation assessment history.
    
    Parameters:
        limit (int): Maximum number of records to return. Defaults to 30; capped at 200.
    
    Returns:
        list[dict[str, Any]]: Assessment records with id, graduated status, overall progress, metrics, and created_at timestamp.
    """
    rows = (
        db.query(GraduationAssessment)
        .filter(GraduationAssessment.user_id == user.id)
        .order_by(GraduationAssessment.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [
        {
            "id": r.id,
            "graduated": r.graduated,
            "overall_progress": r.overall_progress,
            "metrics": json.loads(r.metrics_json or "{}"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/recommendations")
async def generate_real_recommendations(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    Generate advisory recommendations for the real portfolio.
    
    Gated: requires a graduated track record.
    
    Returns:
        A dict containing the report ID, graduation verdict, structured
        recommendations (with ticker, action, confidence, thesis, and risks),
        count of items surfaced to the inbox, and advisory flags (estimate,
        not_tax_advice, not_financial_advice).
    """
    verdict = evaluate_graduation(db, user.id)
    _persist_if_due(db, user.id, verdict)
    # ADR 0025: unlocked by the capital ladder (a candidate at Earned or
    # above), never by the legacy criteria.
    ladder = ladder_state(db, user.id)
    if not any(c["stage"] >= 2 for c in ladder["candidates"]):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_graduated",
                "message": (
                    "No candidate has reached the Earned stage of the capital ladder. "
                    "Recommendations stay locked until one does."
                ),
                "ladder": ladder,
                "graduation": verdict.to_dict(),
                "passive_core": _passive_core_block(db),
            },
        )

    from app.decision.recommendation_engine.models import UserProfile
    from app.decision.recommendation_engine.orchestrator import (
        generate_recommendations as run_recommendations,
    )

    result = await run_recommendations(db, user.id, user_profile=UserProfile())
    report = result["report"]

    return {
        "report_id": getattr(report, "report_id", None),
        "graduation": verdict.to_dict(),
        "recommendations": [
            {
                "ticker": item.ticker,
                "action": item.action,
                "confidence": item.confidence,
                "thesis": item.thesis,
                "risks": list(item.risks),
            }
            for item in getattr(report, "recommendations", [])
        ],
        "estimate": True,
        "not_tax_advice": True,
        "not_financial_advice": True,
    }

