"""The technical rules of the event desk on daily bars (ADR 0028). A thin wrapper: the maths is in
``foundation/technical.py``, shared with the lab replay. Today's bar is provisional: its close is the live
reference price."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.foundation import technical

from . import cfd_instruments

#: Where each exchange's technical evaluation runs, in Berlin time: 20 minutes before its close.
EVALUATION_TIME = {"XETR": (17, 10), "XNYS": (21, 40)}


def with_provisional(bars: list[technical.Bar], ref: float, today: date) -> list[technical.Bar]:
    """Today's bar with *ref* as its close (replaces a stored partial bar, else appends one)."""
    base = [b for b in bars if b.date < today]
    prev = bars[-1] if bars and bars[-1].date == today else None
    o = prev.open if prev else ref
    return [*base, technical.Bar(today, o, max(prev.high if prev else ref, ref), min(prev.low if prev else ref, ref), ref)]


def bars_today(db: Session, inst: cfd_instruments.CfdInstrument, ref: Decimal, today: date) -> list[technical.Bar]:
    """Daily bars with today's provisional bar. A stock's close is the live reference price. An index has
    no quote (its paper price is an ETF proxy), so its provisional bar is the data provider's own daily bar
    for today; without one, no bars (no evaluation)."""
    bars = cfd_instruments.daily_bars(db, inst)
    if inst.kind == "index":
        return bars if bars and bars[-1].date == today else []
    return with_provisional(bars, float(ref), today)


def entry_hits(db: Session, inst: cfd_instruments.CfdInstrument, ref: Decimal, today: date) -> list[technical.RuleHit]:
    bars = bars_today(db, inst, ref, today)
    return technical.rule_hits(bars) if bars else []


def exit_reason(db: Session, inst: cfd_instruments.CfdInstrument, cell: str, direction: str, ref: Decimal,
                today: date) -> str | None:
    bars = bars_today(db, inst, ref, today)
    return technical.exit_hit(cell, direction, bars) if bars else None
