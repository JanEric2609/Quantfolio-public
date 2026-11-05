"""Persist each month's plan actions (ADR 0019 §3).

Snapshots are idempotent per month: re-saving the same month's plan returns
the existing rows. A placed order the next sync finds is linked back to its
action by ``foundation.recommendation_execution.detect_executions``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.decision.monthly_plan import build_monthly_plan
from app.foundation.models.entities import MonthlyPlanAction, User

logger = logging.getLogger(__name__)


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


def snapshot_missing_months(db: Session, *, now: datetime | None = None) -> int:
    """Store this month's plan for each user who has no rows for it yet; returns how many.

    Once a user has rows the month stays as it was first planned, so later
    changes to the plan do not add to it. A month with no actions is retried
    the next day, and a missed day (worker down, no network) catches up.
    """
    month = (now or datetime.now(UTC)).strftime("%Y-%m")
    stored = {
        user_id
        for (user_id,) in db.query(MonthlyPlanAction.user_id)
        .filter(MonthlyPlanAction.month == month)
        .distinct()
    }
    created = 0
    for (user_id,) in db.query(User.id).all():
        if user_id not in stored and snapshot_monthly_plan(db, user_id, now=now):
            created += 1
    return created


def register_monthly_plan_snapshot_job(scheduler: Any | None = None) -> str:
    """Daily at 07:10 UTC, after the 06:30 price backfill."""
    from app.foundation.jobs import register_cron_job

    def _inner() -> None:
        from app.foundation.core.db import SessionLocal

        with SessionLocal() as db:
            logger.info("monthly_plan_snapshot: stored for %s user(s)", snapshot_missing_months(db))

    return register_cron_job("monthly_plan_snapshot", _inner, scheduler=scheduler, hour=7, minute=10)


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
