"""``/idea <TICKER|ISIN> <up|down> [today|1d|2d] [ko|warrant|both] [event]`` (ADR 0027)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.scalable.cli import ScalableCli

from .. import service
from ..underlyings import ALIASES, ISIN_RE, Underlying, UnderlyingUnknown, resolve_underlying

HORIZON_DAYS = {"today": 1, "1d": 2, "2d": 2}
PRODUCT_WORDS = {"ko": ("knockout",), "knockout": ("knockout",), "warrant": ("warrant",), "both": ("knockout", "warrant")}
USAGE = "Usage: /idea <TICKER or ISIN> <up|down> [today|1d|2d] [ko|warrant|both] [event]"


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
        else:
            raise IdeaParseError(f"I don't know '{raw}'. {USAGE}")
    return Idea(target, "long" if side in ("up", "long") else "short", horizon, products, event)


@dataclass
class IdeaOutcome:
    proposals: list[service.ProposalResult]
    underlying: Underlying | None
    error: str | None = None


def _symbol(target: str) -> str | None:
    """The ticker for the earnings lookup: only when the owner typed a ticker, not an ISIN or an index alias."""
    key = target.upper()
    return None if (key in ALIASES or ISIN_RE.fullmatch(key)) else key


def start_idea(db: Session, user_id: str, idea: Idea, cli: ScalableCli, now: datetime) -> IdeaOutcome:
    try:
        underlying = resolve_underlying(db, idea.target, user_id)
    except UnderlyingUnknown as exc:
        return IdeaOutcome([], None, str(exc))
    results = [
        service.propose(
            db, user_id, signal="owner_call", underlying=underlying, direction=idea.direction, product_type=pt,
            cli=cli, now=now, event_flag=idea.event, horizon_days=HORIZON_DAYS[idea.horizon],
            symbol=_symbol(idea.target))
        for pt in idea.product_types
    ]
    return IdeaOutcome(results, underlying)


def format_proposal(result: service.ProposalResult, underlying: Underlying, product_type: str = "") -> str:
    p = result.position
    if p is None or result.nonce is None:
        why = (result.reason or "nothing to propose").replace("_", " ")
        return f"{underlying.label} {product_type}: no proposal ({why})."
    notional = (p.entry_price or 0) * (p.units or 0)
    lines = [
        f"PAPER idea: {underlying.label} {'up' if p.direction == 'long' else 'down'} via {p.product_type}",
        f"{p.isin} ({p.issuer}), {p.units} units at {p.entry_price:.4f}, notional {notional:.2f} EUR",
        f"Fees 2 x 0.99 EUR, spread {float(p.spread_pct or 0) * 100:.2f} %, stop {p.stop_price:.4f}",
        "Paper only: no real order is placed. Tap within 3 minutes.",
    ]
    if result.notes:
        lines.append("News seen: " + " | ".join(result.notes))
    return "\n".join(lines)


def take_markup(position_id: str, nonce: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "Paper: take", "callback_data": f"desk:take:{position_id}:{nonce}"}]]}
