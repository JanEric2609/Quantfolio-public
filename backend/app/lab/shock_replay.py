"""Event study of strong, price-confirmed shock days on daily index bars (research only, never a pass test).

The owner's idea (2026-11-25): trade CFDs rarely, only when something big and clear is happening. The part of that
which has ten years of history is the price footprint of a major headline: an unusually large day that closes at
its extreme. This module measures what an index did after such days. News and trader sentiment have no history here
(the news wire has stored rows only since 2026-11-20), so they can only be forward-tested; see
``docs/plan-strong-events-2026-11-25.md``.

Frozen before any data was read:

* a shock day t for an index: ``|close_t / close_{t-1} - 1| >= K x sigma``, where sigma is the standard deviation of
  the previous 20 daily returns (not including t), and the close is in the outer 25 % of the day's range in the
  direction of the move (price confirmation: no reversal during the day). Two tiers, K = 2 and K = 3;
* the trade enters at the next open, either with the move (``continue``) or against it (``fade``), and exits at the
  close H trading days after the entry day (entry day t+1, exit day t+1+H, so H = 1 holds one night and H = 5 five), H = 1 or 5. Eight cells: 2 tiers x 2 directions x 2 horizons;
* an index is allowed one position at a time (an event inside an open position is skipped);
* costs per trade, in basis points of the position: the registered spread, overnight interest per calendar night held,
  and 0.5 % of the gross result for USD indices (the broker's FX fee, charged on the result);
* significance on one observation per entry date (the mean net result over the indices that traded that day, since
  indices shock together), a t-test, Holm over the eight cells.

Changing a constant after seeing a result makes it a new trial. A pass would justify a new pre-registration and a
paper alert cell, never a live signal. Instruments arrive as plain dicts.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.foundation import technical
from app.lab.signal_replay import FX_FEE, nights

SIGMA_N = 20
CONFIRM = 0.25  # the close sits in the outer quarter of the day's range
TIERS = (2.0, 3.0)
HORIZONS = (1, 5)
MODES = ("continue", "fade")
CELLS = tuple(f"k{int(k)}_{m}_h{h}" for k in TIERS for m in MODES for h in HORIZONS)


@dataclass(frozen=True)
class Shock:
    i: int  # index of the shock day in the bars
    sign: int  # +1 up day, -1 down day
    sigmas: float  # the move in units of the trailing sigma


def shocks(bars: list[technical.Bar]) -> list[Shock]:
    """All confirmed shock days (K = 2 or more; the tier is applied by the caller)."""
    out: list[Shock] = []
    closes = [b.close for b in bars]
    for i in range(SIGMA_N + 1, len(bars)):
        rets = [math.log(closes[j] / closes[j - 1]) for j in range(i - SIGMA_N, i)]
        sd = statistics.pstdev(rets)
        if sd <= 0:
            continue
        move = closes[i] / closes[i - 1] - 1
        z = abs(move) / sd
        b = bars[i]
        rng = b.high - b.low
        if z < TIERS[0] or rng <= 0:
            continue
        pos = (b.close - b.low) / rng
        confirmed = pos >= 1 - CONFIRM if move > 0 else pos <= CONFIRM
        if confirmed:
            out.append(Shock(i, 1 if move > 0 else -1, z))
    return out


def trade_net_bp(bars: list[technical.Bar], s: Shock, mode: str, horizon: int, inst: dict[str, Any]) -> tuple[date, float] | None:
    """(entry date, net bp) of one trade on shock *s*; ``None`` when the data ends before the exit."""
    ei, xi = s.i + 1, s.i + 1 + horizon
    if xi >= len(bars):
        return None
    side = s.sign if mode == "continue" else -s.sign
    entry, exit_ = bars[ei].open, bars[xi].close
    gross = side * (exit_ / entry - 1) * 1e4
    rate = float(inst["overnight_long" if side > 0 else "overnight_short"])
    cost = float(inst["spread_bp"]) + rate * nights(bars[ei].date, bars[xi].date) * 1e4
    if inst.get("usd"):
        cost += FX_FEE * abs(gross)
    return bars[ei].date, gross - cost


def run(bars_by_key: dict[str, list[technical.Bar]], instruments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per cell: events, events a year, mean net bp, day-level t and Holm p, a per-year table."""
    from scipy import stats as st

    res: dict[str, Any] = {}
    p_raw: dict[str, float] = {}
    for tier in TIERS:
        for mode in MODES:
            for h in HORIZONS:
                cell = f"k{int(tier)}_{mode}_h{h}"
                by_date: dict[date, list[float]] = defaultdict(list)
                events = 0
                years: dict[int, list[float]] = defaultdict(list)
                span_years = 0.0
                for key, bars in bars_by_key.items():
                    span_years = max(span_years, (bars[-1].date - bars[0].date).days / 365.25) if bars else span_years
                    busy_until = -1
                    for s in shocks(bars):
                        if s.sigmas < tier or s.i <= busy_until:
                            continue
                        r = trade_net_bp(bars, s, mode, h, instruments[key])
                        if r is None:
                            continue
                        busy_until = s.i + h
                        by_date[r[0]].append(r[1])
                        events += 1
                days = {d: statistics.fmean(v) for d, v in by_date.items()}
                for d, v in days.items():
                    years[d.year].append(v)
                vals = list(days.values())
                n = len(vals)
                sd = statistics.stdev(vals) if n > 1 else 0.0
                t = statistics.fmean(vals) / (sd / math.sqrt(n)) if n > 1 and sd > 0 else None
                res[cell] = {"events": events, "event_days": n,
                             "events_per_year": round(events / span_years, 1) if span_years else None,
                             "mean_net_bp": statistics.fmean(vals) if vals else None, "t_days": t,
                             "hit_rate": sum(1 for v in vals if v > 0) / n if n else None,
                             "per_year": {y: {"n": len(v), "mean_net_bp": statistics.fmean(v)} for y, v in sorted(years.items())}}
                if t is not None and n > 2:
                    p_raw[cell] = float(2 * st.t.sf(abs(t), n - 1))
    running = 0.0
    m = len(CELLS)
    for i, k in enumerate(sorted(p_raw, key=p_raw.__getitem__)):
        running = max(running, min(1.0, (m - i) * p_raw[k]))
        res[k]["p_holm"] = running
    for k in CELLS:
        res[k].setdefault("p_holm", 1.0)
    return res
