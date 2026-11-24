"""The bankroll guard (ADR 0028): the owner's real, logged losses stop the order details, never the paper scoring."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskCfdJournal

from . import calendar as cal
from .event_limits import EVENT_LIMITS


@dataclass(frozen=True)
class Results:
    month_eur: Decimal  # signed: negative is a loss
    day_eur: Decimal
    open_real: int


@dataclass(frozen=True)
class Guard:
    ok: bool
    reason: str
    month_eur: Decimal
    day_eur: Decimal
    resumes: str = ""


def _aware(value: datetime) -> datetime:
    from datetime import UTC
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def real_results(db: Session, user_id: str, now: datetime) -> Results:
    local = now.astimezone(cal.BERLIN)
    rows = db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id).all()
    month = day = Decimal(0)
    open_real = 0
    for r in rows:
        if r.status == "entered":
            open_real += 1
        elif r.status == "closed" and r.real_pnl_eur is not None and r.closed_at is not None:
            at = _aware(r.closed_at).astimezone(cal.BERLIN)
            if (at.year, at.month) == (local.year, local.month):
                month += Decimal(r.real_pnl_eur)
                if at.date() == local.date():
                    day += Decimal(r.real_pnl_eur)
    return Results(month, day, open_real)


def guard(db: Session, user_id: str, now: datetime) -> Guard:
    res = real_results(db, user_id, now)
    local = now.astimezone(cal.BERLIN)
    nxt_month = (local.replace(day=1) + timedelta(days=32)).replace(day=1)
    if -res.month_eur >= EVENT_LIMITS.monthly_real_loss_stop:
        return Guard(False, f"the month's real loss reached {EVENT_LIMITS.monthly_real_loss_stop} EUR",
                     res.month_eur, res.day_eur, nxt_month.date().isoformat())
    if -res.day_eur >= EVENT_LIMITS.daily_real_loss_stop:
        return Guard(False, f"today's real loss reached {EVENT_LIMITS.daily_real_loss_stop} EUR",
                     res.month_eur, res.day_eur, (local.date() + timedelta(days=1)).isoformat())
    if res.open_real >= EVENT_LIMITS.max_open:
        return Guard(False, f"{res.open_real} real positions are open", res.month_eur, res.day_eur, "a position closes")
    return Guard(True, "", res.month_eur, res.day_eur)
