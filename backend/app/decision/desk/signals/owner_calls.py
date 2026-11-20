"""``/idea <TICKER|ISIN> <up|down> [today|1d] [ko|warrant|both] [event]`` (ADR 0027)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.scalable.cli import ScalableCli

from .. import instrument, service
from ..underlyings import Underlying, UnderlyingUnknown, resolve_underlying

#: ``1d`` holds to 21:30 on the next trading day: the desk's maximum (``max_holding_trading_days``).
HORIZON_DAYS = {"today": 1, "1d": 2}
PRODUCT_WORDS = {"ko": ("knockout",), "knockout": ("knockout",), "warrant": ("warrant",), "both": ("knockout", "warrant")}
USAGE = "Usage: /idea <TICKER or ISIN> <up|down> [today|1d] [ko|warrant|both] [event]"


class IdeaParseError(ValueError):
    pass


@dataclass(frozen=True)
class Idea:
    target: str
    direction: str  # long | short
    horizon: str
    product_types: tuple[str, ...]
    event: bool


def parse_idea(text: str) -> Idea:
    words = text.split()
    if words and words[0].lower().startswith("/idea"):
        words = words[1:]
    if len(words) < 2:
        raise IdeaParseError(USAGE)
    target, side = words[0], words[1].lower()
    if side not in ("up", "down", "long", "short"):
        raise IdeaParseError("Say up or down. " + USAGE)
    horizon, products, event = "today", ("knockout", "warrant"), False
    seen: set[str] = set()
    for raw in words[2:]:
        w = raw.lower()
        if w in HORIZON_DAYS and "h" not in seen:
            horizon = w
            seen.add("h")
        elif w in PRODUCT_WORDS and "p" not in seen:
            products = PRODUCT_WORDS[w]
            seen.add("p")
        elif w == "event":
            event = True
        elif w.endswith("d") and w[:-1].isdigit():
            raise IdeaParseError(f"'{raw}' is longer than the desk holds: at most 1d (until 21:30 on the "
                                 f"next trading day). {USAGE}")
        else:
            raise IdeaParseError(f"I don't know '{raw}'. {USAGE}")
    return Idea(target, "long" if side in ("up", "long") else "short", horizon, products, event)


@dataclass
class IdeaOutcome:
    proposals: list[service.ProposalResult]
    underlying: Underlying | None
    error: str | None = None


def start_idea(db: Session, user_id: str, idea: Idea, cli: ScalableCli, now: datetime,
               clock: Callable[[], datetime] | None = None) -> IdeaOutcome:
    """*clock*: the wall clock for a live idea (see ``service.propose``); each proposal reads it again."""
    try:
        underlying = resolve_underlying(db, idea.target, user_id)
    except UnderlyingUnknown as exc:
        return IdeaOutcome([], None, str(exc))
    results = [
        service.propose(
            db, user_id, signal="owner_call", underlying=underlying, direction=idea.direction, product_type=pt,
            cli=cli, now=clock() if clock else now, event_flag=idea.event,
            horizon_days=HORIZON_DAYS[idea.horizon], clock=clock)
        for pt in idea.product_types
    ]
    return IdeaOutcome(results, underlying)


#: Why nothing was proposed, as the owner reads it (Telegram and the Desk page). Unlisted codes are
#: shown with spaces for underscores.
REASON_TEXT = {
    "exit_time_passed": "exit time passed: after 21:30 Berlin a 'today' idea would close at once; send 1d instead",
    "earnings_unknown": "the earnings date could not be read; add 'event' to take it anyway",
    "event_block_earnings": "earnings fall inside the holding time; add 'event' to accept that",
    "event_block_macro": "a central-bank or macro release falls inside the holding time",
    "no_candidate": "no product with a fresh quote and a tight enough spread",
    "no_volatility": "the underlying's volatility could not be read",
    "underlying_closed": "the underlying's own exchange is closed",
    "outside_window": "outside the desk's trading hours (08:05-21:45 Berlin)",
    "after_cutoff": "past the 21:30 cut-off before a weekend or holiday",
    "spread_too_wide": "the spread is too wide",
    "quote_stale": "the quote is too old",
    "risk_over_limit": "the loss at the stop would exceed the per-trade limit",
    "too_small": "not even one unit fits the limits",
    "halted": "the desk is halted",
}


def reason_text(reason: str | None) -> str:
    codes = [c for c in (reason or "").split(",") if c]
    if not codes:
        return "nothing to propose"
    return "; ".join(REASON_TEXT.get(c, c.replace("_", " ")) for c in codes)


def format_proposal(result: service.ProposalResult, underlying: Underlying, product_type: str = "") -> str:
    p = result.position
    if p is None or result.nonce is None:
        return f"{underlying.label} {product_type}: no proposal ({reason_text(result.reason)})."
    notional = (p.entry_price or 0) * (p.units or 0)
    lines = [
        f"PAPER idea: {underlying.label} {'up' if p.direction == 'long' else 'down'} via {p.product_type}",
        *instrument.identity_lines(p),
        instrument.order_line(p) + f", notional {notional:.2f} EUR",
        f"Fees 2 x 0.99 EUR, spread {float(p.spread_pct or 0) * 100:.2f} %, stop {p.stop_price:.4f}",
        "Paper only: no real order is placed. Tap within 3 minutes.",
    ]
    if result.notes:
        lines.append("News seen: " + " | ".join(result.notes))
    return "\n".join(lines)


def take_markup(position_id: str, nonce: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "Paper: take", "callback_data": f"desk:take:{position_id}:{nonce}"}]]}
