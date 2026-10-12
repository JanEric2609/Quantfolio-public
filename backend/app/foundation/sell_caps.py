"""Sell-rate guardrails shared by the advisor sleeve and the LLM mandates.

ADR 0012 caps a single sell at a third of the position; ADR 0013 adds a
trailing-window cumulative cap and a core-holding floor. The advisor cycle
applied them; the mandate review's sells passed no gate at all, so Mandate B
sold 89 % of its book in one review (plan 2026-10-12, D6). Both paths now use
these functions.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.foundation.models.entities import PaperTrade
from app.foundation.models.entities._core import now_utc

#: Per-cycle cap: at most this share of a position per decision (ADR 0012).
MAX_SELL_PCT_OF_POSITION = 1.0 / 3.0
#: Cumulative cap: at most this share of the window-start value (ADR 0013).
MAX_CUMULATIVE_SELL_PCT = 1.0 / 3.0
#: Trailing window of the cumulative cap, calendar days (~20 trading days).
CUMULATIVE_SELL_WINDOW_DAYS = 28


def cumulative_sell_totals(
    db: Session, portfolio_id: str, window_days: int
) -> dict[str, tuple[float, float]]:
    """Per-ticker ``(sell_value_eur, buy_value_eur)`` from trades in the
    trailing *window_days*, used to reconstruct each position's window-start
    value without a daily per-holding price snapshot (none is stored)."""
    cutoff = now_utc() - timedelta(days=window_days)
    rows = (
        db.query(PaperTrade.ticker, PaperTrade.side, PaperTrade.value)
        .filter(PaperTrade.portfolio_id == portfolio_id, PaperTrade.date >= cutoff)
        .all()
    )
    totals: dict[str, list[float]] = {}
    for ticker, side, value in rows:
        if not ticker:
            continue
        bucket = totals.setdefault(ticker.upper(), [0.0, 0.0])
        v = float(value or 0.0)
        if side == "sell":
            bucket[0] += v
        elif side == "buy":
            bucket[1] += v
    return {ticker: (sell_v, buy_v) for ticker, (sell_v, buy_v) in totals.items()}


def effective_sell_cap(
    ticker: str,
    current_value: float,
    total_value: float,
    *,
    max_sell_pct_of_position: float,
    cumulative_totals: dict[str, tuple[float, float]],
    max_cumulative_sell_pct: float,
    instrument_types: dict[str, str],
    core_holding_floor_pct: float,
) -> float:
    """The most a position may be sold this cycle, folding in all three
    guardrails: the per-cycle cap, the trailing-window cumulative cap, and
    (for a diversified/index holding) the core-holding floor. Returns the
    tightest of whichever apply."""
    cap = current_value * max_sell_pct_of_position

    sell_v, buy_v = cumulative_totals.get(ticker.upper(), (0.0, 0.0))
    window_start_value = max(0.0, current_value + sell_v - buy_v)
    cumulative_cap = window_start_value * max_cumulative_sell_pct
    cumulative_remaining = max(0.0, cumulative_cap - sell_v)
    cap = min(cap, cumulative_remaining)

    if instrument_types.get(ticker.upper()) == "etf":
        floor_value = total_value * core_holding_floor_pct
        cap = min(cap, max(0.0, current_value - floor_value))

    return cap
