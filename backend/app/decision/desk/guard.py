"""Pure entry and exit checks (ADR 0027). No database, no network, no clock but the one passed in."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from . import calendar as cal
from .limits import LIMITS

# A reason in this set stops a paper entry too. The others are live-only caps: paper records
# what live would have done and is not capped by them.
LIVE_ONLY_REASONS = frozenset({"max_open", "max_entries", "daily_loss", "pot_exceeded", "outside_window_rule"})


@dataclass(frozen=True)
class DeskState:
    halted: bool = False
    open_live: int = 0
    entries_today: int = 0
    loss_today: Decimal = Decimal(0)  # a positive number: EUR lost so far today
    pot_used: Decimal = Decimal(0)


@dataclass(frozen=True)
class Proposal:
    signal: str  # owner_call | index_momentum
    product_type: str  # knockout | warrant
    units: int
    ask: Decimal
    notional: Decimal
    worst_case_loss: Decimal
    spread_pct: Decimal
    liquid: bool
    quote_age_s: float
    exchange: str = "XETR"
    days_to_expiry: int | None = None
    earnings_in_window: bool = False
    event_flag: bool = False  # the owner said "event": earnings are accepted
    earnings_unknown: bool = False  # a US stock whose earnings calendar could not be read
    macro_events: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    is_index: bool = False


@dataclass(frozen=True)
class Verdict:
    reasons: tuple[str, ...]
    ok: bool  # would pass every live limit
    paper_ok: bool  # would pass every limit that also stops a paper entry

    @property
    def paper_reasons(self) -> tuple[str, ...]:
        return tuple(r for r in self.reasons if r not in LIVE_ONLY_REASONS)


def check_entry(state: DeskState, p: Proposal, now: datetime) -> Verdict:
    reasons: list[str] = []
    local = now.astimezone(cal.BERLIN)
    if state.halted:
        reasons.append("halted")
    if p.units < 1:
        reasons.append("too_small")
    if p.worst_case_loss > LIMITS.max_loss_per_trade:
        reasons.append("risk_over_limit")
    if p.quote_age_s > LIMITS.max_quote_age_s:
        reasons.append("quote_stale")
    cap = LIMITS.spread_cap_liquid if p.liquid else LIMITS.spread_cap_other
    if p.spread_pct > cap:
        reasons.append("spread_too_wide")
    if p.product_type == "warrant" and (p.days_to_expiry is None or p.days_to_expiry < LIMITS.warrant_min_days_to_expiry):
        reasons.append("warrant_too_near_expiry")
    if p.earnings_in_window and not p.event_flag:
        reasons.append("event_block_earnings")
    elif p.earnings_unknown and not p.event_flag:
        reasons.append("earnings_unknown")
    if p.is_index and p.macro_events:
        reasons.append("event_block_macro")
    if not cal.underlying_session_open(p.exchange, now) and p.signal == "owner_call":
        reasons.append("underlying_closed")
    if cal.flat_by_day(local.date()) and local >= cal.cutoff_at(local.date()):
        reasons.append("after_cutoff")
    if not cal.in_trading_window(now):
        reasons.append("outside_window" if p.signal == "owner_call" else "outside_window_rule")
    if state.open_live >= LIMITS.max_open_live:
        reasons.append("max_open")
    if state.entries_today >= LIMITS.max_entries_per_day:
        reasons.append("max_entries")
    if state.loss_today >= LIMITS.daily_loss:
        reasons.append("daily_loss")
    if state.pot_used + p.notional > LIMITS.pot:
        reasons.append("pot_exceeded")
    paper = [r for r in reasons if r not in LIVE_ONLY_REASONS]
    return Verdict(tuple(reasons), not reasons, not paper)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands stored times back without a zone; they are UTC."""
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


def check_exit(position: Any, bid: Decimal | None, now: datetime, *, halted: bool = False) -> str | None:
    """The exit reason, or ``None`` to keep holding.

    ``stop``: the bid is at or under the stop. ``horizon``: past the latest exit. ``friday_cutoff`` /
    ``holiday_cutoff``: 21:30 Berlin on the last trading day before a weekend or a holiday.
    ``quote_lost``: no fresh quote for 10 minutes inside today's trading window (paper closes at the last bid).
    ``owner_close`` is an explicit action, not a tick decision; so is ``knock_out`` (no bid and the
    underlying past the barrier: closed at 0 by the service, which can read the underlying).
    """
    if halted:
        return "halt"
    if bid is not None and position.stop_price is not None and bid <= Decimal(position.stop_price):
        return "stop"
    max_exit = _aware(position.max_exit_at)
    if max_exit is not None and now >= max_exit:
        return "horizon"
    local = now.astimezone(cal.BERLIN)
    if cal.flat_by_day(local.date()) and local >= cal.cutoff_at(local.date()):
        return cal.cutoff_kind(local.date())
    if bid is None and cal.in_trading_window(now):
        seen = _aware(position.last_quote_at or position.entry_at)
        # Counted inside today's window only: an overnight position's last quote is from last night,
        # and the issuers' first quotes of the morning may take a few minutes.
        opened = cal.window_start_at(local.date())
        if seen is not None and seen < opened:
            seen = opened
        if seen is not None and now - seen >= timedelta(seconds=LIMITS.quote_lost_s):
            return "quote_lost"
    return None
