"""The owner's journal of the manual Trading 212 CFD trades (ADR 0027 amendment 2026-11-23).

One row per paper CFD signal. The owner says "entered", "skipped" or "closed" (Telegram buttons, ``/cfd``,
or the Intraday page) and may give the fill and the quantity. This is the owner's own record: it is NOT
the hash-chained ledger, never writes a ``desk_events`` row and never changes a ``DeskPosition``. The
paper cell ``index_momentum:cfd`` is scored on the rule's prices, whatever the owner did.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskCfdJournal, DeskPosition

from . import calendar as cal

logger = logging.getLogger(__name__)

#: A fill more than this far from the estimated index level is a typo, not a price.
LEVEL_TOLERANCE = Decimal("0.20")
MAX_QUANTITY = Decimal(1000)
FX_FEE_ON_RESULT = Decimal("0.005")  # Trading 212's fee on a USD market's result (broker fact, see cfd.T212)
SHORT_ID = re.compile(r"[0-9a-f][0-9a-f-]{7,35}")


class JournalError(ValueError):
    """The input is refused; the message is safe to show the owner."""


def parse_number(raw: str) -> Decimal:
    """``24500.5`` and ``24500,5``; both separators in one number is refused (no thousands separators)."""
    value = raw.strip()
    if "." in value and "," in value:
        raise JournalError("Use one decimal separator and no thousands separator, e.g. 24500.5")
    try:
        number = Decimal(value.replace(",", "."))
    except ArithmeticError as exc:
        raise JournalError(f"'{raw[:20]}' is not a number.") from exc
    if not number.is_finite():
        raise JournalError(f"'{raw[:20]}' is not a number.")
    return number


def create_for_signal(db: Session, position: DeskPosition, ctx: Any, *, key: str) -> DeskCfdJournal:
    """The journal row of a new paper CFD (same savepoint as the position). *ctx* is a ``cfd.CfdContext`` or
    ``None`` (typed ``Any`` so that this module never imports ``cfd``: ``cfd`` imports it)."""
    row = DeskCfdJournal(
        position_id=position.id, user_id=position.user_id, market_key=key, direction=position.direction,
        currency="USD" if (position.underlying_isin or "").startswith("US") else "EUR", status="sent",
        exit_at=position.max_exit_at)
    if ctx is not None:
        row.r1, row.index_level_est, row.eur_per_unit = ctx.r1, ctx.index_level_est, ctx.eur_per_unit
        row.quantity_suggested, row.exposure_eur = ctx.quantity_t212, ctx.exposure_eur
        row.stop_points, row.typical_30m_points, row.spread_points = (
            ctx.stop_points, ctx.typical_30m_points, ctx.spread_points)
        row.enter_by, row.exit_at, row.currency = ctx.enter_by, ctx.exit_at, ctx.currency
    db.add(row)
    db.flush()
    return row


def for_position(db: Session, position_id: str) -> DeskCfdJournal | None:
    return db.query(DeskCfdJournal).filter(DeskCfdJournal.position_id == position_id).one_or_none()


def _check_level(journal: DeskCfdJournal, level: Decimal | None) -> None:
    if level is None:
        return
    if level <= 0:
        raise JournalError("The level must be above 0.")
    est = Decimal(journal.index_level_est) if journal.index_level_est else None
    if est and abs(level / est - 1) > LEVEL_TOLERANCE:
        raise JournalError(f"{level:,.1f} is more than 20 % away from the index near {est:,.0f}: check the number.")


def _check_quantity(quantity: Decimal | None) -> None:
    if quantity is not None and not (0 < quantity <= MAX_QUANTITY):
        raise JournalError("The quantity must be above 0 and at most 1000.")


def mark_entered(db: Session, journal: DeskCfdJournal, now: datetime, level: Decimal | None = None,
                 quantity: Decimal | None = None) -> DeskCfdJournal:
    if journal.status == "closed":
        raise JournalError("That trade is already closed.")
    _check_level(journal, level)
    _check_quantity(quantity)
    journal.status = "entered"
    if journal.entered_at is None:
        journal.entered_at = now
    if level is not None:
        journal.entry_level = level
    if quantity is not None:
        journal.quantity = quantity
    elif journal.quantity is None:
        journal.quantity = journal.quantity_suggested
    db.flush()
    return journal


def mark_skipped(db: Session, journal: DeskCfdJournal, now: datetime) -> DeskCfdJournal:
    if journal.status != "sent":
        raise JournalError("Only a trade you have not answered yet can be skipped.")
    journal.status = "skipped"
    db.flush()
    return journal


def mark_closed(db: Session, journal: DeskCfdJournal, now: datetime, level: Decimal | None = None) -> DeskCfdJournal:
    if journal.status in ("closed", "skipped"):
        raise JournalError(f"That trade is {journal.status}.")
    _check_level(journal, level)
    if journal.entered_at is None:
        journal.entered_at = now
    journal.status, journal.closed_at = "closed", now
    if level is not None:
        journal.exit_level = level
    if journal.quantity is None:
        journal.quantity = journal.quantity_suggested
    journal.real_pnl_eur = real_pnl(db, journal)
    db.flush()
    return journal


def real_pnl(db: Session, journal: DeskCfdJournal) -> Decimal | None:
    """Signed points x quantity x EUR per unit of the close day (ECB), less 0.5 % of the result on a USD
    market. Overnight interest is not included (see :func:`nights_held`)."""
    if journal.entry_level is None or journal.exit_level is None or not journal.quantity:
        return None
    points = Decimal(journal.exit_level) - Decimal(journal.entry_level)
    if journal.direction != "long":
        points = -points
    rate = Decimal(1)
    if journal.currency != "EUR":
        from app.foundation.ecb_fx import eur_per_unit

        day = (journal.closed_at or datetime.now(UTC)).astimezone(cal.BERLIN).date()
        value, _src = eur_per_unit(db, journal.currency, day)
        rate = Decimal(str(value)) if value else Decimal(journal.eur_per_unit or 0)
        if rate <= 0:
            return None
    result = points * Decimal(journal.quantity) * rate
    if journal.currency != "EUR":
        result -= FX_FEE_ON_RESULT * abs(result)
    return result.quantize(Decimal("0.0001"))


def nights_held(journal: DeskCfdJournal) -> int | None:
    """Berlin calendar days between the owner's entry and close (overnight interest is charged per night)."""
    if journal.entered_at is None or journal.closed_at is None:
        return None
    a = _aware(journal.entered_at).astimezone(cal.BERLIN).date()
    b = _aware(journal.closed_at).astimezone(cal.BERLIN).date()
    return max((b - a).days, 0)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def stop_level(journal: DeskCfdJournal) -> Decimal | None:
    """The stop for the owner's own fill: the fill minus (long) or plus (short) the stop in points."""
    if journal.entry_level is None or journal.stop_points is None:
        return None
    entry, pts = Decimal(journal.entry_level), Decimal(journal.stop_points)
    return entry - pts if journal.direction == "long" else entry + pts


def find_for_owner(db: Session, user_id: str, ref: str | None, now: datetime | None = None) -> DeskCfdJournal | None:
    """With no *ref*: the newest row of today (Berlin) that is not closed or skipped. With a *ref*: the row
    whose position id starts with it (8 or more characters, a literal prefix, no LIKE)."""
    rows = db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id).order_by(
        DeskCfdJournal.created_at.desc()).all()
    if ref:
        prefix = ref.lower()
        if not SHORT_ID.fullmatch(prefix):
            return None
        matches = [r for r in rows if r.position_id.lower().startswith(prefix)]
        return matches[0] if len(matches) == 1 else None
    today = (now or datetime.now(UTC)).astimezone(cal.BERLIN).date()
    for row in rows:
        if _aware(row.created_at).astimezone(cal.BERLIN).date() == today and row.status not in ("closed", "skipped"):
            return row
    return None


def today_rows(db: Session, user_id: str, now: datetime) -> list[DeskCfdJournal]:
    today = now.astimezone(cal.BERLIN).date()
    return [r for r in db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id).order_by(
        DeskCfdJournal.created_at).all() if _aware(r.created_at).astimezone(cal.BERLIN).date() == today]


