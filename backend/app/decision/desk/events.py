"""The event desk's engine (ADR 0028): technical rules, hype, news drift and the owner's CFD calls on paper.

One stage of the monitor tick, after the index rule: every read first (prices, bars, screener, news), then one
write phase (exits, then entries, each in a savepoint), a commit, and the messages built from stored rows.
Nothing here places an order: ``would_take_live`` stays False and the owner trades by hand.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.foundation import technical
from app.foundation.models.entities import DeskCfdJournal, DeskPosition

from . import calendar as cal
from . import cfd, cfd_instruments, cfd_journal, event_messages, event_rules, event_sizing, event_sources, news, service
from .event_limits import EVENT_LIMITS
from .event_sources import Candidate
from .ledger import append_event
from .prereg_events import EVENT_CELLS, EVENT_HASH, register_events

logger = logging.getLogger(__name__)
EVAL_WINDOW = timedelta(minutes=10)
PRIORITY = {"owner_call:cfd": 0, "news_drift:cfd": 1, "hype_fade:cfd": 2}
ROLLOVER_LEAD = timedelta(minutes=60)


@dataclass
class Reads:
    prices: dict[str, Decimal | None] = field(default_factory=dict)
    tech_exits: dict[str, str] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    fx: Decimal | None = None
    marks: dict[str, str] = field(default_factory=dict)


@dataclass
class _Ctx:  # what ``cfd_journal.create_for_signal`` reads off a context
    r1: Decimal
    index_level_est: Decimal
    eur_per_unit: Decimal
    quantity_t212: Decimal
    exposure_eur: Decimal
    stop_points: Decimal
    typical_30m_points: Decimal
    spread_points: Decimal
    enter_by: datetime | None
    exit_at: datetime
    currency: str


@dataclass
class Outcome:
    opened: list[str] = field(default_factory=list)  # position ids
    closed: list[tuple[str, str]] = field(default_factory=list)  # (position id, reason)
    skipped: list[str] = field(default_factory=list)


def _inst_of(db: Session, journal: DeskCfdJournal | None) -> cfd_instruments.CfdInstrument | None:
    if journal is None:
        return None
    return cfd_instruments.by_key(journal.market_key) or cfd_instruments.for_symbol(
        db, journal.market_key.split(":", 1)[-1])


def open_event_positions(db: Session) -> list[DeskPosition]:
    return db.query(DeskPosition).filter(DeskPosition.status == "open", DeskPosition.mode == service.MODE,
                                         DeskPosition.cell.in_(EVENT_CELLS)).order_by(DeskPosition.created_at).all()


# -- evaluation times ---------------------------------------------------------------


def evaluation_time(exchange: str, day) -> datetime | None:
    """20 minutes before the session's close (Xetra 17:10 Berlin; NYSE 21:40 Berlin, earlier on an early
    close and in the weeks the two clocks differ)."""
    times = cal.session_times(day, exchange)
    return None if times is None else times[1].astimezone(UTC) - timedelta(minutes=20)


def due_exchanges(db: Session, now: datetime) -> list[str]:
    health = service.health(db)
    out = []
    for ex in ("XETR", "XNYS"):
        day = now.astimezone(cal.BERLIN).date() if ex == "XETR" else now.astimezone(event_sources.NY).date()
        at = evaluation_time(ex, day)
        if at is not None and at <= now < at + EVAL_WINDOW and health.get(f"event_eval_{ex}") != day.isoformat():
            out.append(ex)
    return out


def hype_slot(db: Session, now: datetime) -> str | None:
    local = now.astimezone(event_sources.NY)
    if not event_sources.in_hype_window(now) or local.minute % 15 != 0:
        return None
    slot = local.strftime("%Y-%m-%dT%H:%M")
    return None if service.health(db).get("event_hype_slot") == slot else slot


# -- read phase ---------------------------------------------------------------------


def _price(db: Session, cli: Any, inst: cfd_instruments.CfdInstrument, now: datetime, cache: dict[str, Decimal | None],
           errors: list[str] | None) -> Decimal | None:
    if inst.key not in cache:
        cache[inst.key] = cfd_instruments.reference_price(db, cli, inst, now, errors)
    return cache[inst.key]


def read_phase(db: Session, cli: Any, now: datetime, errors: list[str]) -> Reads:
    r = Reads()
    r.fx = cfd._eur_per_usd(db, now)
    held = open_event_positions(db)
    journals = {p.id: cfd_journal.for_position(db, p.id) for p in held}
    insts = {p.id: _inst_of(db, journals[p.id]) for p in held}
    for p in held:
        held_inst = insts[p.id]
        if held_inst is not None and now.astimezone(cal.BERLIN).hour >= 8:
            _price(db, cli, held_inst, now, r.prices, errors)
    due = due_exchanges(db, now)
    held_keys = {i.key for i in insts.values() if i is not None}
    for ex in due:
        today = now.astimezone(cal.BERLIN if ex == "XETR" else event_sources.NY).date()
        r.marks[f"event_eval_{ex}"] = today.isoformat()
        for inst in cfd_instruments.UNIVERSE:
            if inst.underlying.exchange != ex:
                continue
            ref = _price(db, cli, inst, now, r.prices, errors)
            if ref is None:
                continue
            try:
                _technical_reads(db, inst, ref, today, now, r, held, insts, held_keys)
            except (SQLAlchemyError, ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError, OSError) as exc:
                db.rollback()
                logger.info("desk event technical read failed: %s", type(exc).__name__)
    slot = hype_slot(db, now)
    try:
        news_c = event_sources.prepare_news(db, cli, now, errors)
        r.candidates.extend(news_c)
        if slot:
            r.marks["event_hype_slot"] = slot
            exclude = {c.inst.underlying.symbol for c in news_c if c.inst.underlying.symbol}
            r.candidates.extend(event_sources.prepare_hype(db, news.alpaca_for(db), now, exclude, errors))
    except (SQLAlchemyError, ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
        db.rollback()
        logger.info("desk event source read failed: %s", type(exc).__name__)
    return r


def _technical_reads(db: Session, inst: cfd_instruments.CfdInstrument, ref: Decimal, today, now: datetime, r: Reads,
                     held: list[DeskPosition], insts: dict[str, Any], held_keys: set[str]) -> None:
    bars = event_rules.bars_today(db, inst, ref, today)
    if not bars:
        return
    for p in held:
        if insts[p.id] is not None and insts[p.id].key == inst.key:
            why = technical.exit_hit(p.cell, p.direction, bars)
            if why:
                r.tech_exits[p.id] = why
    if inst.key in held_keys:
        return
    completed = [b for b in bars if b.date < today]
    atr = technical.atr(completed, technical.EVENT_RULES["atr"])
    if not atr:
        return
    level = Decimal(str(bars[-1].close)) if inst.kind == "index" else None
    for hit in technical.rule_hits(bars):
        r.candidates.append(Candidate(
            cell=hit.cell, inst=inst, direction=hit.direction, ref_price=ref,
            stop_distance=Decimal(str(round(hit.stop_atr * atr, 6))),
            max_exit_at=event_sources.exit_after_days(now, hit.max_hold_days, inst.underlying.exchange),
            reason=f"{hit.reason}.", evidence={"stop_atr": hit.stop_atr}, level=level))


# -- write phase --------------------------------------------------------------------


def _nights(position: DeskPosition, now: datetime) -> int:
    a = service._aware(position.entry_at)
    return max(0, (now.astimezone(cal.BERLIN).date() - a.astimezone(cal.BERLIN).date()).days) if a else 0


def event_close(db: Session, position: DeskPosition, price: Decimal, reason: str, now: datetime,
                inst: cfd_instruments.CfdInstrument) -> bool:
    """Paper exit at the reference price. Costs: the spread, the FX fee on a USD result and overnight interest
    per night held (Friday counts 3, as the calendar nights between entry and exit)."""
    if not service.claim_open(db, position):
        return False
    entry, qty = Decimal(position.entry_price or 0), Decimal(position.quantity or 0)
    exposure = entry * qty
    raw = price / entry - 1 if entry > 0 else Decimal(0)
    move = raw if position.direction == "long" else -raw
    gross = move * exposure
    fx_fee = abs(gross) * event_sizing.FX_FEE if inst.usd else Decimal(0)
    nights = _nights(position, now)
    overnight = inst.overnight_rate(position.direction) * exposure * nights
    spread_cost = Decimal(position.spread_pct or 0) * exposure
    position.exit_price, position.exit_at, position.exit_reason = price, now.astimezone(UTC), reason
    position.exit_fee = (fx_fee + overnight).quantize(Decimal("0.0001"))
    position.net_eur = (gross - spread_cost - fx_fee - overnight).quantize(Decimal("0.0001"))
    position.underlying_move = move
    position.status = "closed"
    append_event(db, "exit", service.MODE, {
        "reason": reason, "price": str(price), "product_type": "cfd", "cell": position.cell,
        "gross_eur": str(gross.quantize(Decimal("0.0001"))), "spread_eur": str(spread_cost.quantize(Decimal("0.0001"))),
        "fx_fee_eur": str(fx_fee.quantize(Decimal("0.0001"))), "overnight_eur": str(overnight.quantize(Decimal("0.0001"))),
        "nights": nights, "net_eur": str(position.net_eur), "underlying_move": str(move)}, position.id, now=now)
    return True


def owner_close(db: Session, position: DeskPosition, cli: Any, now: datetime) -> bool:
    """``/close`` or the Closed button on an event position: a fresh reference price, else nothing is closed."""
    inst = _inst_of(db, cfd_journal.for_position(db, position.id))
    if inst is None:
        return False
    price = cfd_instruments.reference_price(db, cli, inst, now)
    if price is None or price <= 0:
        return False
    return event_close(db, position, price, "owner_close", now, inst)


def _stopped(position: DeskPosition, price: Decimal) -> bool:
    return cfd._stopped(position, price)


def _exits(db: Session, now: datetime, reads: Reads, halted: bool, out: Outcome) -> None:
    for p in open_event_positions(db):
        journal = cfd_journal.for_position(db, p.id)
        inst = _inst_of(db, journal)
        if inst is None:
            continue
        price = reads.prices.get(inst.key)
        if price is not None and price > 0:
            p.last_bid, p.last_quote_at = price, now.astimezone(UTC)
        reason = None
        exit_at = service._aware(p.max_exit_at)
        if halted:
            reason = "halt"
        elif price is not None and price > 0 and _stopped(p, price):
            reason = "stop"
        elif inst.kind == "index" and now >= cfd_instruments.index_expiry(inst.key, now.astimezone(cal.BERLIN).date()) \
                .astimezone(UTC) - ROLLOVER_LEAD:
            reason = "rollover"
        elif p.id in reads.tech_exits:
            reason = reads.tech_exits[p.id]
        elif exit_at is not None and now >= exit_at:
            reason = "time"
        if reason is None:
            continue
        px = price if price is not None and price > 0 else None
        if px is None:
            if reason != "halt" or Decimal(p.last_bid or 0) <= 0:
                continue  # wait for a fresh reference price (the exchange opens again)
            px = Decimal(str(p.last_bid))
        if event_close(db, p, px, reason, now, inst):
            out.closed.append((p.id, reason))


def _session_ok(inst: cfd_instruments.CfdInstrument, now: datetime) -> bool:
    ex = inst.underlying.exchange
    if not cal.underlying_session_open(ex, now):
        return False
    day = now.astimezone(event_sources.NY if ex == "XNYS" else cal.BERLIN).date()
    if cal.is_early_close(day, ex):
        times = cal.session_times(day, ex)
        if times and now > times[0].astimezone(UTC) + (times[1] - times[0]) * 2 / 3:
            return False
    return True


def _open_one(db: Session, user_id: str, cand: Candidate, now: datetime, fx: Decimal | None,
              ordered: bool = False) -> tuple[DeskPosition | None, str]:
    """Filter, size and open one paper position (a savepoint); returns it or the reason it was skipped."""
    inst = cand.inst
    if service.is_halted(db):
        return None, "halted"
    held = open_event_positions(db)
    if len(held) >= EVENT_LIMITS.max_open:
        return None, "max_open"
    day_start = now.astimezone(cal.BERLIN).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    recent = db.query(DeskPosition).filter(DeskPosition.cell.in_(EVENT_CELLS), DeskPosition.mode == service.MODE,
                                           DeskPosition.created_at >= now.astimezone(UTC) - timedelta(
                                               minutes=EVENT_LIMITS.min_minutes_between_same_instrument)).all()
    if sum(1 for p in recent if (service._aware(p.created_at) or now) >= day_start) >= EVENT_LIMITS.max_new_per_day:
        return None, "max_new_per_day"
    if any(p.underlying_isin == inst.underlying.isin for p in [*held, *recent]):
        return None, "instrument_busy"
    if not _session_ok(inst, now):
        return None, "session_closed"
    eur = Decimal(1) if inst.currency == "EUR" else fx
    if eur is None or eur <= 0:
        return None, "no_fx"
    level = cand.level or cand.ref_price
    open_margin = Decimal(0)
    for p in held:
        j = cfd_journal.for_position(db, p.id)
        i2 = _inst_of(db, j)
        if j is not None and i2 is not None and j.exposure_eur:
            open_margin += Decimal(j.exposure_eur) * i2.margin_rate
    sizing = event_sizing.size(inst, level, cand.stop_distance, eur, now, cand.direction, open_margin)
    if sizing is None:
        return None, "sizing"
    scale = cand.ref_price / level
    paper_stop = cand.ref_price - cand.stop_distance * scale if cand.direction == "long" \
        else cand.ref_price + cand.stop_distance * scale
    signal = cand.cell.split(":")[0]
    paper_only = event_messages.paper_only_reason(db, user_id, cand.cell, now)
    try:
        with db.begin_nested():
            position = DeskPosition(
                user_id=user_id, mode=service.MODE, cell=cand.cell, signal=signal, product_type="cfd",
                isin=inst.underlying.isin, issuer="Trading 212", underlying_isin=inst.underlying.isin,
                underlying_label=inst.underlying.label, direction=cand.direction, liquid=True,
                leverage_or_omega=Decimal(1), units=None,
                quantity=(sizing.exposure_eur / cand.ref_price).quantize(Decimal("0.00000001")),
                entry_price=cand.ref_price, entry_fee=Decimal(0), entry_at=now.astimezone(UTC),
                stop_price=paper_stop.quantize(Decimal("0.0001")), max_exit_at=cand.max_exit_at.astimezone(UTC),
                spread_pct=inst.spread_bp / Decimal(10000), underlying_entry=cand.ref_price, last_bid=cand.ref_price,
                last_quote_at=now.astimezone(UTC), status="open", would_take_live=False, prereg_hash=EVENT_HASH,
                product_name=f"{inst.t212_name} CFD (Trading 212, manual)", created_at=now.astimezone(UTC))
            db.add(position)
            db.flush()
            expiry = ""
            if inst.kind == "index":
                expiry = cfd_instruments.index_expiry(inst.key, now.astimezone(cal.BERLIN).date()) \
                    .strftime("%Y-%m-%d at %H:%M Berlin")
            append_event(db, "fill", service.MODE, {
                "product_type": "cfd", "cell": cand.cell, "market": inst.t212_name, "reference_price": str(cand.ref_price),
                "level": str(level), "exposure_eur": str(sizing.exposure_eur), "spread_bp": str(inst.spread_bp),
                "stop": str(position.stop_price), "would_take_live": False, "manual": True,
                "max_exit_at": cand.max_exit_at.isoformat(), "reason": cand.reason[:300], "evidence": cand.evidence,
                "paper_only": paper_only, "loss_at_stop": str(sizing.loss_at_stop_eur),
                "stop_text": (f"{cand.evidence['stop_atr']} ATR" if "stop_atr" in cand.evidence
                              else str(cand.evidence.get("stop", "1.5 ATR"))),
                "expiry": expiry}, position.id, now=now)
            ctx = _Ctx(
                r1=Decimal(0), index_level_est=level, eur_per_unit=eur, quantity_t212=sizing.quantity,
                exposure_eur=sizing.exposure_eur, stop_points=cand.stop_distance, typical_30m_points=Decimal(0),
                spread_points=level * inst.spread_bp / Decimal(10000), enter_by=None, exit_at=cand.max_exit_at,
                currency=inst.currency)
            cfd_journal.create_for_signal(db, position, ctx, key=inst.key)
    except SQLAlchemyError as exc:
        logger.warning("desk event open failed: %s", type(exc).__name__)
        return None, "db_error"
    return position, ""


def write_phase(db: Session, user_id: str, now: datetime, reads: Reads) -> Outcome:
    out = Outcome()
    halted = service.is_halted(db)
    _exits(db, now, reads, halted, out)
    if halted:
        return out
    for cand in sorted(reads.candidates, key=lambda c: PRIORITY.get(c.cell, 3)):
        position, why = _open_one(db, user_id, cand, now, reads.fx)
        if position is not None:
            out.opened.append(position.id)
        else:
            out.skipped.append(f"{cand.cell}:{cand.inst.key}:{why}")
    return out


def messages(db: Session, now: datetime, out: Outcome) -> list[Any]:
    """The texts of a stage, built from stored rows after the commit (no network read)."""
    from .outmsg import OutMsg, cfd_markup

    texts: list[Any] = []
    for pid, reason in out.closed:
        p = db.get(DeskPosition, pid)
        if p is not None:
            texts.append(event_messages.close_text(db, p, reason, cfd_journal.for_position(db, pid)))
    for pid in out.opened:
        p = db.get(DeskPosition, pid)
        j = cfd_journal.for_position(db, pid)
        if p is not None and j is not None:
            texts.append(OutMsg(event_messages.live_text(db, p, j, now), cfd_markup(j), j.id))
    return texts


def run_stage(db: Session, user_id: str, cli: Any, now: datetime, errors: list[str]) -> list[Any]:
    """Reads, then writes, one commit; returns the messages to send."""
    register_events(db, now=now)
    reads = read_phase(db, cli, now, errors)
    out = write_phase(db, user_id, now, reads)
    if reads.marks:
        service.set_health(db, **reads.marks)
    db.commit()
    return messages(db, now, out)


def open_owner_call(db: Session, user_id: str, cand: Candidate, cli: Any, now: datetime) -> tuple[list[Any], str]:
    """A ``/idea ... cfd`` call: its candidate was read by the caller; the FX read happens here, before any write."""
    register_events(db, now=now)
    fx = cfd._eur_per_usd(db, now) if cand.inst.currency != "EUR" else Decimal(1)
    out = Outcome()
    position, why = _open_one(db, user_id, cand, now, fx)
    if position is None:
        db.rollback()
        return [], why
    out.opened.append(position.id)
    db.commit()
    return messages(db, now, out), ""


SKIP_TEXT = {
    "halted": "The desk is halted.", "max_open": "Three event positions are already open.",
    "max_new_per_day": "Four new event positions today already.",
    "instrument_busy": "That instrument has an event position open or opened in the last 24 hours.",
    "session_closed": "Its exchange is closed (or too late in an early-close session).",
    "no_fx": "No EUR/USD rate to size it.", "sizing": "Nothing fits the 5 EUR risk and the margin limit at that stop.",
    "db_error": "A database error stopped it; nothing was opened.",
}


def expiry_warning(today) -> list[str]:
    """Index contracts whose next expiry is only assumed and falls within 14 days (a Control Center warning)."""
    out = []
    for key in cfd_instruments.INDEX_EXPIRY:
        known = datetime.fromisoformat(cfd_instruments.INDEX_EXPIRY[key]).date()
        if known < today and cfd_instruments.assumed_expiry(today).date() - today <= timedelta(days=14):
            out.append(key)
    return out


cfd.EVENT_OWNER_CLOSE = owner_close
