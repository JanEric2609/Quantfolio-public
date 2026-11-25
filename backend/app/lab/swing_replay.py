"""Replay of the proposed ``swing_trend`` cell on daily index bars (research only, never a pass test).

Rule, frozen before the data was read (2026-11-25, docs/plan-swing-desk-ml-2026-11-25.md section 6):

* signal at the close of day t: the sign of the 20-day return, and the 1-day return has the same sign
  ("already moving"); otherwise no trade;
* entry at the open of day t+1; short or long, one position per instrument at a time;
* stop: a trailing 3 x ATR(14) (ATR taken at entry), moved only in the trade's favour, checked on the daily
  high/low; a gap through the stop fills at the open (that is the gap risk, counted);
* time exit at the close after 10 trading days;
* size ``risk / (stop distance + costs per unit)``, costs as ``signal_replay`` (spread, overnight interest per
  calendar night held, so a Friday-to-Monday hold pays 3 nights, 0.5 % FX on a USD result).

``flipped=True`` trades the opposite side on the same days: a control that shows whether the sign carries
anything. Changing a constant after seeing a result makes it a new trial. Instruments arrive as plain dicts.
"""
from __future__ import annotations

from typing import Any

from app.foundation import technical
from app.lab.signal_replay import FX_FEE, RTrade, nights, summarize

LOOKBACK = 20
ATR_N = 14
STOP_ATR = 3.0
MAX_HOLD = 10
WARMUP = 60
CELL = "swing_trend:cfd"


def simulate(key: str, bars: list[technical.Bar], inst: dict[str, Any], risk: float = 5.0,
             flipped: bool = False) -> tuple[list[RTrade], dict[str, int]]:
    """Trades and a small counter: ``stops`` filled, ``gap_overruns`` filled worse than the stop level."""
    out: list[RTrade] = []
    gaps = {"stops": 0, "gap_overruns": 0}
    spread = float(inst["spread_bp"]) / 1e4
    usd = bool(inst.get("usd"))
    pos: dict[str, Any] | None = None
    pending: str | None = None
    for i in range(WARMUP, len(bars)):
        bar = bars[i]
        if pos is None and pending is not None:
            d = pending
            pending = None
            a = technical.atr(bars[:i], ATR_N)
            if a:
                px = bar.open
                dist = STOP_ATR * a
                rate = float(inst["overnight_long" if d == "long" else "overnight_short"])
                per_unit = dist + spread * px + (FX_FEE * dist if usd else 0.0) + rate * px * round(MAX_HOLD * 7 / 5)
                pos = {"date": bar.date, "i": i, "px": px, "dir": d, "dist": dist, "qty": risk / per_unit,
                       "stop": px - dist if d == "long" else px + dist, "best": px}
        if pos is not None and i >= pos["i"]:
            d = pos["dir"]
            exit_px, reason = None, None
            if d == "long":
                if bar.low <= pos["stop"]:
                    exit_px, reason = min(pos["stop"], bar.open), "stop"
                    gaps["stops"] += 1
                    gaps["gap_overruns"] += int(bar.open < pos["stop"])
            elif bar.high >= pos["stop"]:
                exit_px, reason = max(pos["stop"], bar.open), "stop"
                gaps["stops"] += 1
                gaps["gap_overruns"] += int(bar.open > pos["stop"])
            if exit_px is None and i - pos["i"] >= MAX_HOLD:
                exit_px, reason = bar.close, "time"
            if exit_px is not None:
                sign = 1 if d == "long" else -1
                gross = sign * (exit_px - pos["px"]) * pos["qty"]
                rate = float(inst["overnight_long" if d == "long" else "overnight_short"])
                cost = spread * pos["px"] * pos["qty"] + rate * pos["px"] * pos["qty"] * nights(pos["date"], bar.date)
                fx = FX_FEE * abs(gross) if usd else 0.0
                out.append(RTrade(key, CELL, d, pos["date"], bar.date, gross - cost - fx,
                                  (bar.date - pos["date"]).days, reason or "time"))
                pos = None
                continue
            # trail in the trade's favour from today's close (checked from tomorrow)
            if d == "long":
                pos["best"] = max(pos["best"], bar.close)
                pos["stop"] = max(pos["stop"], pos["best"] - pos["dist"])
            else:
                pos["best"] = min(pos["best"], bar.close)
                pos["stop"] = min(pos["stop"], pos["best"] + pos["dist"])
        if pos is None and pending is None and i + 1 < len(bars):
            r20 = bar.close / bars[i - LOOKBACK].close - 1
            r1 = bar.close / bars[i - 1].close - 1
            if r20 != 0 and r1 != 0 and (r20 > 0) == (r1 > 0):
                side = "long" if r20 > 0 else "short"
                if flipped:
                    side = "short" if side == "long" else "long"
                pending = side
    return out, gaps


def run(bars_by_key: dict[str, list[technical.Bar]], instruments: dict[str, dict[str, Any]], risk: float = 5.0) -> dict[str, Any]:
    """The cell and its flipped control, pooled and per instrument, with the gap counters."""
    res: dict[str, Any] = {}
    for name, flipped in (("swing_trend", False), ("flipped_control", True)):
        per: dict[str, list[RTrade]] = {}
        gap_total = {"stops": 0, "gap_overruns": 0}
        for key, bars in bars_by_key.items():
            trades, gaps = simulate(key, bars, instruments[key], risk, flipped)
            per[key] = trades
            for k in gap_total:
                gap_total[k] += gaps[k]
        res[name] = {"pooled": summarize([t for ts in per.values() for t in ts]),
                     "per_instrument": {k: summarize(ts) for k, ts in sorted(per.items())}, "gaps": gap_total}
    return res
