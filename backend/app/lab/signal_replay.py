"""Replay of the event desk's technical rules on daily bars, 2016-2026 (ADR 0028; research, never the pass test).

Uses ``foundation/technical.py`` (the same code the desk runs). The instruments arrive as plain dicts so the lab
never imports ``decision``. Costs as registered: the instrument's spread, overnight interest per calendar night
held (so a Friday close-to-Monday hold pays 3 nights), 0.5 % FX on the result of a USD instrument. Size is
``risk_per_trade / (stop distance + costs per unit)``. EUR/USD is taken as 1: results are in quote currency.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.foundation import technical
from app.foundation.quant_metrics import record_trial

CELLS = ("ta_trend:cfd", "ta_band:cfd", "ta_rsi2:cfd")
FX_FEE = 0.005
WARMUP = 210


@dataclass(frozen=True)
class RTrade:
    key: str
    cell: str
    direction: str
    entry: date
    exit: date
    net: float
    hold_days: int
    reason: str


def nights(entry: date, exit_: date) -> int:
    """Calendar nights held: Friday to Monday is 3."""
    return max(0, (exit_ - entry).days)


def simulate(key: str, bars: list[technical.Bar], inst: dict[str, Any], risk: float = 5.0,
             rules: dict[str, Any] | None = None, cells: tuple[str, ...] = CELLS) -> list[RTrade]:
    """One cell at a time per instrument: a new entry waits until the previous trade of that cell closed."""
    rules = rules or technical.EVENT_RULES
    out: list[RTrade] = []
    spread = float(inst["spread_bp"]) / 1e4
    usd = bool(inst.get("usd"))
    for cell in cells:
        pos: dict[str, Any] | None = None
        for i in range(WARMUP, len(bars)):
            bar = bars[i]
            if pos is not None:
                held = (bar.date - pos["date"]).days
                trading_days = i - pos["i"]
                stop, d = pos["stop"], pos["dir"]
                exit_px, reason = None, None
                if d == "long" and bar.low <= stop:
                    exit_px, reason = min(stop, bar.open), "stop"
                elif d == "short" and bar.high >= stop:
                    exit_px, reason = max(stop, bar.open), "stop"
                else:
                    why = technical.exit_hit(cell, d, bars[: i + 1], rules)
                    if why:
                        exit_px, reason = bar.close, why
                    elif trading_days >= pos["max_hold"]:
                        exit_px, reason = bar.close, "time"
                if exit_px is not None:
                    sign = 1 if d == "long" else -1
                    gross = sign * (exit_px - pos["px"]) * pos["qty"]
                    night_rate = float(inst["overnight_long" if d == "long" else "overnight_short"])
                    cost = spread * pos["px"] * pos["qty"] + night_rate * pos["px"] * pos["qty"] * nights(pos["date"], bar.date)
                    fx = FX_FEE * abs(gross) if usd else 0.0
                    out.append(RTrade(key, cell, d, pos["date"], bar.date, gross - cost - fx, held, reason or 'time'))
                    pos = None
                continue
            hits = [h for h in technical.rule_hits(bars[: i + 1], rules) if h.cell == cell]
            if not hits:
                continue
            h = hits[0]
            a = technical.atr(bars[: i + 1], rules["atr"])
            if not a:
                continue
            dist = h.stop_atr * a
            px = bar.close
            night = float(inst["overnight_long" if h.direction == "long" else "overnight_short"]) * px
            per_unit = dist + spread * px + (FX_FEE * dist if usd else 0.0) + night
            qty = risk / per_unit
            stop = px - dist if h.direction == "long" else px + dist
            pos = {"date": bar.date, "i": i, "px": px, "dir": h.direction, "stop": stop, "qty": qty,
                   "max_hold": h.max_hold_days}
    return out


def summarize(trades: list[RTrade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0, "mean_eur": None, "t": None, "hit_rate": None, "mean_hold_days": None, "max_drawdown_eur": 0.0,
                "per_year": {}}
    nets = [t.net for t in trades]
    mean = statistics.fmean(nets)
    sd = statistics.stdev(nets) if n > 1 else 0.0
    tstat = mean / (sd / math.sqrt(n)) if sd > 0 else None
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x.exit):
        eq += t.net
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    years: dict[str, dict[str, float]] = {}
    for t in trades:
        y = years.setdefault(str(t.exit.year), {"n": 0, "total_eur": 0.0})
        y["n"] += 1
        y["total_eur"] += t.net
    return {"n": n, "mean_eur": mean, "t": tstat, "hit_rate": sum(1 for x in nets if x > 0) / n,
            "mean_hold_days": statistics.fmean(t.hold_days for t in trades), "max_drawdown_eur": dd,
            "per_year": {k: {"n": int(v["n"]), "total_eur": v["total_eur"]} for k, v in sorted(years.items())}}


def run(bars_by_key: dict[str, list[technical.Bar]], instruments: dict[str, dict[str, Any]], risk: float = 5.0,
        stock_spread_bp: float | None = None) -> dict[str, Any]:
    """Per cell: pooled and per instrument. *stock_spread_bp* overrides every stock's spread (the stress case)."""
    per: dict[str, dict[str, list[RTrade]]] = {c: {} for c in CELLS}
    for key, bars in bars_by_key.items():
        inst = dict(instruments[key])
        if stock_spread_bp is not None and inst.get("kind") == "stock":
            inst["spread_bp"] = stock_spread_bp
        for t in simulate(key, bars, inst, risk):
            per[t.cell].setdefault(key, []).append(t)
    return {cell: {"pooled": summarize([t for ts in by.values() for t in ts]),
                   "per_instrument": {k: summarize(ts) for k, ts in sorted(by.items())}}
            for cell, by in per.items()}


def record_trials(db: Session, event_hash: str, cells: tuple[str, ...] = CELLS) -> int:
    """One research trial per cell (context desk_events, a hash of its own: not the registration's)."""
    for cell in cells:
        h = hashlib.sha256(f"replay:{event_hash}:{cell}".encode("ascii")).hexdigest()
        record_trial(db, "desk_events", f"replay:{cell}:{event_hash[:12]}", {"cell": cell, "kind": "replay"},
                     config_hash=h)
    return len(cells)
