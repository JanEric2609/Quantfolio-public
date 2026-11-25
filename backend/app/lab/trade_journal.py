"""Where do the owner's real CFD results come from? Pure analysis of a closed-trade list (research only).

Input is a list of :class:`JournalTrade` (built from a Trading 212 history CSV by ``scripts/cfd_journal_report.py``).
Nothing here reads the database, talks to a broker or sends a message.

The headline is the payoff ratio (average loss / average win) and the hit rate it demands:
a rule that wins 1.5 EUR and loses 4.2 EUR needs 73 % winners just to break even, before any luck.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class JournalTrade:
    instrument: str
    direction: str  # long | short
    opened: datetime | None
    closed: datetime
    result_eur: float
    open_price: float | None = None
    close_price: float | None = None
    quantity: float | None = None
    exit_type: str | None = None  # STOP LOSS | TAKE PROFIT | TRAILING STOP | MARKET (your own tap)
    spread_eur: float | None = None  # the round trip's spread cost in EUR (per-unit column x units)
    spread_bp: float | None = None  # the spread in basis points of the price

    @property
    def hold_minutes(self) -> float | None:
        return (self.closed - self.opened).total_seconds() / 60 if self.opened else None

    @property
    def move_points(self) -> float | None:
        """Adverse (negative) or favourable move in index points, in the trade's direction."""
        if self.open_price is None or self.close_price is None:
            return None
        sign = 1 if self.direction == "long" else -1
        return sign * (self.close_price - self.open_price)


def _block(trades: list[JournalTrade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t.result_eur for t in trades if t.result_eur > 0]
    losses = [-t.result_eur for t in trades if t.result_eur < 0]
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    payoff = avg_loss / avg_win if avg_win > 0 and avg_loss > 0 else None
    return {
        "n": n,
        "total_eur": round(sum(t.result_eur for t in trades), 2),
        "hit_rate": round(len(wins) / n, 3),
        "avg_win_eur": round(avg_win, 2),
        "avg_loss_eur": round(avg_loss, 2),
        "payoff_loss_per_win": round(payoff, 2) if payoff else None,
        "break_even_hit_rate": round(1 / (1 + 1 / payoff), 3) if payoff else None,
        "expectancy_eur": round(statistics.fmean(t.result_eur for t in trades), 3),
        "spread_paid_eur": round(sum(t.spread_eur or 0.0 for t in trades), 2),
        "median_spread_bp": round(statistics.median(bps), 1) if (bps := [t.spread_bp for t in trades if t.spread_bp is not None]) else None,
    }


def _group(trades: list[JournalTrade], key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[JournalTrade]] = defaultdict(list)
    for t in trades:
        k = key(t)
        if k is not None:
            groups[str(k)].append(t)
    return {k: _block(v) for k, v in sorted(groups.items())}


def _hold_bucket(t: JournalTrade) -> str | None:
    m = t.hold_minutes
    if m is None:
        return None
    return "0-5 min" if m < 5 else "5-30 min" if m < 30 else "30-120 min" if m < 120 else "over 2 h"


def analyse(trades: list[JournalTrade]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda t: t.closed)
    out: dict[str, Any] = {"overall": _block(ordered)}
    if not ordered:
        return out
    out["by_instrument"] = _group(ordered, lambda t: t.instrument)
    out["by_direction"] = _group(ordered, lambda t: t.direction)
    out["by_hour_opened"] = _group(ordered, lambda t: f"{t.opened.hour:02d}h UTC" if t.opened else None)
    out["by_hold_time"] = _group(ordered, _hold_bucket)
    out["by_exit_type"] = _group(ordered, lambda t: t.exit_type)
    pts_w = [t.move_points for t in ordered if t.move_points is not None and t.result_eur > 0]
    pts_l = [-t.move_points for t in ordered if t.move_points is not None and t.result_eur < 0]
    if pts_w and pts_l:
        out["points"] = {"avg_win_points": round(statistics.fmean(pts_w), 1), "avg_loss_points": round(statistics.fmean(pts_l), 1)}
    worst = sorted((t.result_eur for t in ordered))[:3]
    out["three_worst_eur"] = [round(x, 2) for x in worst]
    total = sum(t.result_eur for t in ordered)
    out["worst_three_share_of_net"] = round(sum(worst) / total, 2) if total < 0 else None
    run = best = 0
    for t in ordered:
        run = run + 1 if t.result_eur < 0 else 0
        best = max(best, run)
    out["longest_losing_streak"] = best
    out["reading"] = _reading(out["overall"])
    return out


def _reading(o: dict[str, Any]) -> str:
    if not o.get("payoff_loss_per_win"):
        return "Not enough wins and losses to compare them."
    need, have = o["break_even_hit_rate"], o["hit_rate"]
    if have >= need:
        return f"Hit rate {have:.0%} is above the {need:.0%} the payoff needs."
    return (f"Average loss is {o['payoff_loss_per_win']:.1f} times the average win, so {need:.0%} of trades must win to break even; "
            f"{have:.0%} did. Fix the payoff first: wider targets or smaller stops than the noise allows, not more signals.")
