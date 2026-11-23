"""The owner's journal of the manual Trading 212 CFD trades (ADR 0027 amendment 2026-11-23).

One row per paper CFD signal. The owner says "entered", "skipped" or "closed" (Telegram buttons, ``/cfd``,
or the Intraday page) and may give the fill and the quantity. This is the owner's own record: it is NOT
the hash-chained ledger, never writes a ``desk_events`` row and never changes a ``DeskPosition``. The
paper cell ``index_momentum:cfd`` is scored on the rule's prices, whatever the owner did.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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
#: No index is near this level; it also keeps level x quantity inside ``real_pnl_eur`` (Numeric(14, 4)).
MAX_LEVEL = Decimal(1_000_000)
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
    if level <= 0 or level > MAX_LEVEL:
        raise JournalError("The level must be above 0 and below 1,000,000.")
    est = Decimal(journal.index_level_est) if journal.index_level_est else None
    if est and abs(level / est - 1) > LEVEL_TOLERANCE:
        raise JournalError(f"{level:,.1f} is more than 20 % away from {journal.market_key} near {est:,.0f}: "
                           "check the number.")


@dataclass(frozen=True)
class ReplyFill:
    """What a plain reply to a trade's message says: the entry and/or exit level, the quantity, or skip."""
    entry: Decimal | None = None
    exit: Decimal | None = None
    quantity: Decimal | None = None
    skip: bool = False


_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)?")


def parse_reply(journal: DeskCfdJournal, text: str) -> ReplyFill:
    """Read a reply such as ``7020.5``, ``7020.5 0.0400``, ``7020.5 7005.5``, ``in 7020.5``,
    ``out 7005.5`` or ``skip`` (a leading ``/cfd`` and the trade's own id are ignored). A number near the
    trade's index level is a level (the first is the entry, the second the exit; one level alone is the
    entry until the trade has one); a small number is the quantity. Anything else is refused with the
    reason, so a typo such as ``702.05`` for 7,020.5 is never saved as a quantity."""
    words = text.replace("/cfd", " ").split()
    action = None
    numbers: list[Decimal] = []
    for word in words:
        low = word.lower()
        if low in ("in", "out", "skip"):
            if action is not None:
                raise JournalError("Say in, out or skip once.")
            action = low
        elif journal.position_id.lower().startswith(low) and len(low) >= 8:
            continue
        elif _NUMBER.fullmatch(word) or ("." in word and "," in word):
            numbers.append(parse_number(word))
        else:
            raise JournalError(f"'{word[:20]}' is not a price. Reply with the price, e.g. in <price> or out <price>.")
    if action == "skip":
        if numbers:
            raise JournalError("Skip takes no number.")
        return ReplyFill(skip=True)
    est = Decimal(journal.index_level_est) if journal.index_level_est else None
    suggested = Decimal(journal.quantity_suggested) if journal.quantity_suggested else None
    max_qty = min(MAX_QUANTITY, suggested * 50) if suggested else MAX_QUANTITY
    levels: list[Decimal] = []
    quantity: Decimal | None = None
    for n in numbers:
        if n > 0 and (near_level(journal, n) if est else n > max_qty):
            levels.append(n)
        elif 0 < n <= max_qty and quantity is None:
            quantity = n
        else:
            near = f" or a price near {est:,.0f}" if est else ""
            raise JournalError(f"{n:,} is neither a quantity{near} for {journal.market_key}: check the number.")
    if len(levels) > 2 or (action and len(levels) > 1):
        raise JournalError("Too many prices: send the entry, then the exit.")
    if not levels and quantity is None:
        raise JournalError("Reply with your price, e.g. in <price> [quantity] or out <price>.")
    if action == "out":
        if quantity is not None:
            raise JournalError("The quantity goes with the entry: in <price> <quantity>.")
        if not levels:
            raise JournalError("Reply with your exit price: out <price>.")
        return ReplyFill(exit=levels[0])
    if len(levels) == 2:
        return ReplyFill(entry=levels[0], exit=levels[1], quantity=quantity)
    if not levels:
        return ReplyFill(quantity=quantity)
    if action == "in" or (journal.entry_level is None and journal.status != "closed"):
        return ReplyFill(entry=levels[0], quantity=quantity)
    if journal.entry_level is None and journal.exit_level is None:
        raise JournalError("Send both prices: <entry price> <exit price>.")
    if quantity is not None and journal.entry_level is not None:
        raise JournalError("The quantity goes with the entry: in <price> <quantity>.")
    if journal.exit_level is None:
        return ReplyFill(exit=levels[0])
    if journal.entry_level is None:
        return ReplyFill(entry=levels[0], quantity=quantity)
    raise JournalError("This trade has both prices already.")


def _check_quantity(quantity: Decimal | None) -> None:
    if quantity is not None and not (0 < quantity <= MAX_QUANTITY):
        raise JournalError("The quantity must be above 0 and at most 1000.")


def mark_entered(db: Session, journal: DeskCfdJournal, now: datetime, level: Decimal | None = None,
                 quantity: Decimal | None = None) -> DeskCfdJournal:
    # A row the "Closed" button closed before any price was sent may still get its entry (and quantity).
    late_entry = journal.status == "closed" and journal.entry_level is None and level is not None
    if journal.status == "closed" and not late_entry:
        raise JournalError("That trade is already closed.")
    _check_level(journal, level)
    _check_quantity(quantity)
    if late_entry:
        journal.entry_level = level
        if quantity is not None:
            journal.quantity = quantity
        journal.real_pnl_eur = real_pnl(db, journal)
        db.flush()
        return journal
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
    # A row closed by the button without a level may get its exit level afterwards (``/cfd out <level> <id>``).
    adding_level = journal.status == "closed" and journal.exit_level is None and level is not None
    if journal.status == "skipped" or (journal.status == "closed" and not adding_level):
        raise JournalError(f"That trade is {journal.status}.")
    _check_level(journal, level)
    if journal.entered_at is None:
        journal.entered_at = now
    journal.status = "closed"
    if journal.closed_at is None:
        journal.closed_at = now
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


#: What each ``/cfd`` action needs of a row: ``in`` a row without its fill, ``out`` an entered row or one the
#: "Closed" button closed without its exit level, ``skip`` a row not answered yet.
def _needs(row: DeskCfdJournal, action: str | None) -> bool:
    if action == "in":
        return row.status in ("sent", "entered", "closed") and row.entry_level is None
    if action == "out":
        return row.status == "entered" or (row.status == "closed" and row.exit_level is None)
    if action == "skip":
        return row.status == "sent"
    return row.status != "skipped" and not (row.status == "closed" and row.exit_level is not None)


def near_level(row: DeskCfdJournal, level: Decimal) -> bool:
    """*level* is within the typo tolerance of the row's estimated index level (no estimate: unknown, True)."""
    est = Decimal(row.index_level_est) if row.index_level_est else None
    return est is None or abs(level / est - 1) <= LEVEL_TOLERANCE


def find_for_owner(db: Session, user_id: str, ref: str | None, now: datetime | None = None,
                   action: str | None = None, level: Decimal | None = None) -> DeskCfdJournal | None:
    """With a *ref*: the row whose position id starts with it (8 or more characters, a literal prefix, no
    LIKE). With no *ref*: among today's rows (Berlin) that *action* applies to, the one that fits. A
    *level* keeps the rows whose index is near it (S&P 500 and Nasdaq-100 are far apart), and ``in``
    prefers a row the owner already marked "Entered". Two or more left: :class:`JournalError` naming
    their ids (never a guess: on 2026-11-23 the newest row took an S&P fill meant for the other one)."""
    rows = db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id).order_by(
        DeskCfdJournal.created_at.desc()).all()
    if ref:
        prefix = ref.lower()
        if not SHORT_ID.fullmatch(prefix):
            return None
        matches = [r for r in rows if r.position_id.lower().startswith(prefix)]
        return matches[0] if len(matches) == 1 else None
    today = (now or datetime.now(UTC)).astimezone(cal.BERLIN).date()
    cands = [r for r in rows if _aware(r.created_at).astimezone(cal.BERLIN).date() == today and _needs(r, action)]
    if action is None:
        return cands[0] if cands else None
    if level is not None and level > 0 and cands:
        near = [r for r in cands if near_level(r, level)]
        if not near:
            levels = ", ".join(f"{r.market_key} near {Decimal(r.index_level_est):,.0f}" for r in cands
                               if r.index_level_est)
            raise JournalError(f"{level:,} is not near any of today's trades ({levels}): check the number.")
        cands = near
    if action == "in":
        cands = [r for r in cands if r.status == "entered"] or cands
    if len(cands) > 1:
        ids = ", ".join(f"{r.market_key} {r.position_id[:8]}" for r in cands)
        raise JournalError(f"More than one trade fits ({ids}). Add the id, e.g. /cfd {action} <price> "
                           f"{cands[0].position_id[:8]}, or reply to that trade's message.")
    return cands[0] if cands else None


def slippage_points(journal: DeskCfdJournal) -> Decimal | None:
    """The owner's fill against the signal's estimated index level, in points, signed so that a worse fill
    is positive (a long bought higher, a short sold lower). Includes the gap between Trading 212's
    futures-based price and the cash index, so it is a running record, not a cost."""
    if journal.entry_level is None or not journal.index_level_est:
        return None
    diff = Decimal(journal.entry_level) - Decimal(journal.index_level_est)
    return diff if journal.direction == "long" else -diff


def today_rows(db: Session, user_id: str, now: datetime) -> list[DeskCfdJournal]:
    today = now.astimezone(cal.BERLIN).date()
    return [r for r in db.query(DeskCfdJournal).filter(DeskCfdJournal.user_id == user_id).order_by(
        DeskCfdJournal.created_at).all() if _aware(r.created_at).astimezone(cal.BERLIN).date() == today]


