"""Persist each month's plan actions (ADR 0019 §3).

Snapshots are idempotent per month: re-saving the same month's plan returns
the existing rows. A placed order the next sync finds is linked back to its
action by ``foundation.recommendation_execution.detect_executions``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.decision.monthly_plan import build_monthly_plan
from app.foundation.models.entities import MonthlyPlanAction


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def snapshot_monthly_plan(
    db: Session, user_id: str, *, now: datetime | None = None
) -> list[MonthlyPlanAction]:
    """Build this month's plan and store one row per action. The caller commits."""
    plan = build_monthly_plan(db, user_id, now=now)
    rows: list[MonthlyPlanAction] = []
    for action in plan["actions"]:
        isin = _norm(action.get("isin"))
        ticker = _norm(action.get("ticker"))
        broker = str(action.get("broker") or "")
        account_id = action.get("account_id")
        row = (
            db.query(MonthlyPlanAction)
            .filter_by(
                user_id=user_id, month=plan["month"], sleeve=action["sleeve"],
                kind=action["kind"], isin=isin, ticker=ticker, broker=broker,
                account_id=account_id,
            )
            .first()
        )
        if row is None:
            row = MonthlyPlanAction(
                user_id=user_id, month=plan["month"], sleeve=action["sleeve"],
                kind=action["kind"], amount_eur=float(action.get("amount_eur") or 0.0),
                instrument=str(action.get("instrument") or "")[:128],
                ticker=ticker, isin=isin, broker=broker,
                account_id=account_id,
                detail_json={"note": str(action.get("note") or "")},
            )
            db.add(row)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def open_actions_for(
    db: Session, user_id: str, *, isin: str = "", ticker: str = ""
) -> list[MonthlyPlanAction]:
    """Open plan actions matching an executed trade, newest month first."""
    query = db.query(MonthlyPlanAction).filter(
        MonthlyPlanAction.user_id == user_id,
        MonthlyPlanAction.status == "open",
    )
    if isin:
        query = query.filter(MonthlyPlanAction.isin == isin)
    elif ticker:
        query = query.filter(MonthlyPlanAction.ticker == ticker)
    else:
        return []
    return query.order_by(MonthlyPlanAction.month.desc()).all()


def serialize(row: MonthlyPlanAction) -> dict[str, Any]:
    return {
        "id": row.id,
        "month": row.month,
        "sleeve": row.sleeve,
        "kind": row.kind,
        "amount_eur": row.amount_eur,
        "instrument": row.instrument,
        "ticker": row.ticker or None,
        "isin": row.isin or None,
        "broker": row.broker or None,
        "account_id": row.account_id,
        "status": row.status,
        "fulfilled_at": row.fulfilled_at.isoformat() if row.fulfilled_at else None,
        "note": (row.detail_json or {}).get("note", ""),
    }
