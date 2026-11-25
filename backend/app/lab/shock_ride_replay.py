"""Strong-day entries with state-based exits only, on daily index bars (research only, never a pass test).

Why this exists (docs/research/2026-11-25-event-cfd-edge-research.md): the owner wants to trade only on strong days
and to drop the fixed holding time. Under efficient prices no stopping rule can create an edge (a trailing stop or a
"close back through the average" exit changes the *shape* of the payoff, not its expectation), so an exit rule only
earns anything if returns after strong days are serially dependent. This replay asks that question directly and
keeps a control that separates "the entry day carries information" from "the exit rule is flattering any random
entry": the same exits on random dates (the placebo).

Frozen before the data was read (2026-11-25). Changing a constant after seeing a result makes it a new trial.

* strong day t: ``|r_t| / sigma_{t-1} >= Z_EWMA`` with ``sigma`` the EWMA (lambda 0.94) of daily returns up to the
  day before; or, for the *implied* trigger, ``|r_t| / (VIX_{t-1} / 100 / sqrt(252)) >= Z_IMPLIED`` (how big the
  move was against what options priced the day before);
* entry at the open of t+1, one position per instrument at a time (a trigger while in a position is ignored);
* direction ``with`` trades the strong day's sign, ``fade`` the opposite;
* exit ``chandelier``: a trailing 3 x ATR(14) stop (ATR taken at entry), moved only in the trade's favour, checked on
  the daily high/low, a gap through it fills at the open; exit ``ema_cross``: the same initial 3 x ATR stop, plus
  exit at the close that crosses back through the 10-day EMA relative to where the signal day closed;
* **no time exit.** A position still open at the end of the data is closed at the last close and tagged
  ``data_end`` (censoring, reported, not a rule);
* costs: spread, overnight interest per calendar night held, 0.5 % FX on a USD result (as ``signal_replay``);
  size ``risk / (stop distance + costs per unit)`` with ``SIZING_NIGHTS`` nights of financing assumed for sizing
  only (it is not an exit).

Trials declared: 2 directions x 2 exits x 2 triggers (the implied trigger only where a volatility index is given)
= up to 8. The decision rule (``verdict``): pooled n >= 100, mean net > 0, Holm-adjusted one-sided p < 0.05 over the
cells run, placebo p < 0.05, and at least 60 % of calendar years with 3+ trades positive. Otherwise "null".
A pass would justify a new pre-registration, never a live signal. Instruments arrive as plain dicts.
"""
from __future__ import annotations

import math
import random
import statistics
from datetime import date
from typing import Any

from app.foundation import technical
from app.lab.signal_replay import FX_FEE, RTrade, nights, summarize

Z_EWMA = 2.5
Z_IMPLIED = 1.75
EWMA_LAMBDA = 0.94
ATR_N = 14
STOP_ATR = 3.0
EMA_N = 10
WARMUP = 60
SIZING_NIGHTS = 14
MIN_TRADES = 100
YEAR_POSITIVE_SHARE = 0.6
TREND_MIN_20D = 0.02  # a trend counts as in force at a 2 % 20-day move and 1 % off the 50-day mean
TREND_MIN_SMA = 0.01
EXITS = ("chandelier", "ema_cross")
DIRECTIONS = ("with", "fade")


def ewma_sigma(closes: list[float]) -> list[float | None]:
    """``out[t]`` is the EWMA daily-return sigma using returns up to and including day t (None before 20 returns)."""
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    out: list[float | None] = [None] * len(closes)
    if len(rets) < 20:
        return out
    var = statistics.pvariance(rets[:20])
    for k in range(20, len(rets) + 1):
        if k > 20:
            var = EWMA_LAMBDA * var + (1 - EWMA_LAMBDA) * rets[k - 1] ** 2
        out[k] = math.sqrt(var)  # return k-1 ends at close index k
    return out


def strong_signals(bars: list[technical.Bar], direction: str, trigger: str = "ewma",
                   implied: dict[date, float] | None = None) -> dict[int, str]:
    """Signal index (the strong day's close) -> side to enter at the next open."""
    closes = [b.close for b in bars]
    sig = ewma_sigma(closes)
    out: dict[int, str] = {}
    for i in range(max(WARMUP, 21), len(bars) - 1):
        r = closes[i] / closes[i - 1] - 1
        if r == 0:
            continue
        if trigger == "implied":
            vix = (implied or {}).get(bars[i - 1].date)
            if not vix:
                continue
            z, need = abs(r) / (vix / 100 / math.sqrt(252)), Z_IMPLIED
        else:
            s = sig[i - 1]
            if not s:
                continue
            z, need = abs(r) / s, Z_EWMA
        if z >= need:
            up = r > 0
            out[i] = ("long" if up else "short") if direction == "with" else ("short" if up else "long")
    return out


def established(bars: list[technical.Bar], i: int) -> bool:
    """A strong day inside a trend already in force *before* it: the day before, the 20-day return and the close against
    the 50-day mean both pointed the same way as the strong day ("joining an established selloff" when it is down)."""
    if i < 51:
        return False
    c = [b.close for b in bars[: i + 1]]
    day = 1 if c[-1] > c[-2] else -1
    prev = c[:-1]
    r20 = prev[-1] / prev[-21] - 1
    gap = prev[-1] / (sum(prev[-50:]) / 50) - 1
    return abs(r20) >= TREND_MIN_20D and abs(gap) >= TREND_MIN_SMA and (r20 > 0) == (gap > 0) == (day > 0)


def _ema_aligned(closes: list[float], n: int = EMA_N) -> list[float | None]:
    e = technical.ema_series(closes, n)
    return [None] * (n - 1) + e


def simulate_signals(key: str, bars: list[technical.Bar], inst: dict[str, Any], signals: dict[int, str],
                     exit_mode: str, risk: float = 5.0, tag: str = "", stop_atr: float = STOP_ATR, ema_n: int = EMA_N,
                     atr_n: int = ATR_N, ema_side: str = "signal") -> tuple[list[RTrade], dict[str, int]]:
    out: list[RTrade] = []
    gaps = {"stops": 0, "gap_overruns": 0, "data_end": 0}
    spread = float(inst["spread_bp"]) / 1e4
    usd = bool(inst.get("usd"))
    closes = [b.close for b in bars]
    ema = _ema_aligned(closes, ema_n) if exit_mode == "ema_cross" else []
    cell = f"{tag or 'ride'}:{exit_mode}"
    pos: dict[str, Any] | None = None
    pending: tuple[str, int] | None = None

    def close_out(bar: technical.Bar, px: float, reason: str) -> None:
        nonlocal pos
        assert pos is not None
        d = pos["dir"]
        sign = 1 if d == "long" else -1
        gross = sign * (px - pos["px"]) * pos["qty"]
        rate = float(inst["overnight_long" if d == "long" else "overnight_short"])
        cost = spread * pos["px"] * pos["qty"] + rate * pos["px"] * pos["qty"] * nights(pos["date"], bar.date)
        fx = FX_FEE * abs(gross) if usd else 0.0
        out.append(RTrade(key, cell, d, pos["date"], bar.date, gross - cost - fx, (bar.date - pos["date"]).days, reason))
        pos = None

    for i in range(WARMUP, len(bars)):
        bar = bars[i]
        if pos is None and pending is not None:
            d, sig_i = pending
            pending = None
            a = technical.atr(bars[:i], atr_n)
            if a:
                px = bar.open
                dist = stop_atr * a
                rate = float(inst["overnight_long" if d == "long" else "overnight_short"])
                per_unit = dist + spread * px + (FX_FEE * dist if usd else 0.0) + rate * px * SIZING_NIGHTS
                side0 = 0
                if ema_side == "direction":
                    side0 = 1 if d == "long" else -1  # exit when the close crosses the EMA against the position
                else:
                    e_sig = ema[sig_i] if exit_mode == "ema_cross" else None
                    if e_sig is not None:
                        side0 = 1 if closes[sig_i] > e_sig else -1
                pos = {"date": bar.date, "i": i, "px": px, "dir": d, "dist": dist, "qty": risk / per_unit,
                       "stop": px - dist if d == "long" else px + dist, "best": px, "side0": side0}
        if pos is not None and i >= pos["i"]:
            d = pos["dir"]
            exit_px, reason = None, ""
            if d == "long" and bar.low <= pos["stop"]:
                exit_px, reason = min(pos["stop"], bar.open), "stop"
                gaps["stops"] += 1
                gaps["gap_overruns"] += int(bar.open < pos["stop"])
            elif d == "short" and bar.high >= pos["stop"]:
                exit_px, reason = max(pos["stop"], bar.open), "stop"
                gaps["stops"] += 1
                gaps["gap_overruns"] += int(bar.open > pos["stop"])
            e_now = ema[i] if exit_mode == "ema_cross" else None
            if exit_px is None and e_now is not None and pos["side0"]:
                now = 1 if bar.close > e_now else -1
                if now != pos["side0"]:
                    exit_px, reason = bar.close, "ema_cross"
            if exit_px is None and i == len(bars) - 1:
                exit_px, reason = bar.close, "data_end"
                gaps["data_end"] += 1
            if exit_px is not None:
                close_out(bar, exit_px, reason)
                continue
            if exit_mode == "chandelier":
                if d == "long":
                    pos["best"] = max(pos["best"], bar.close)
                    pos["stop"] = max(pos["stop"], pos["best"] - pos["dist"])
                else:
                    pos["best"] = min(pos["best"], bar.close)
                    pos["stop"] = min(pos["stop"], pos["best"] + pos["dist"])
        if pos is None and pending is None and i in signals and i + 1 < len(bars):
            pending = (signals[i], i)
    return out, gaps


def leg_stats(bars: list[technical.Bar], signals_with: dict[int, str]) -> dict[str, Any]:
    """After a strong day, where does the next day's move sit: the overnight gap (close -> next open) or after the
    open (open -> close)? Signed by the strong day's direction (positive = continuation), in basis points, against the
    unsigned average of every day (the drift a plain long holder earns in each leg)."""
    def stat(xs: list[float]) -> dict[str, Any]:
        n = len(xs)
        if n < 2:
            return {"n": n, "mean_bp": None, "t": None}
        sd = statistics.stdev(xs)
        m = statistics.fmean(xs)
        return {"n": n, "mean_bp": m * 1e4, "t": (m / (sd / math.sqrt(n))) if sd > 0 else None}

    on_all = [bars[i + 1].open / bars[i].close - 1 for i in range(WARMUP, len(bars) - 1)]
    id_all = [bars[i + 1].close / bars[i + 1].open - 1 for i in range(WARMUP, len(bars) - 1)]
    on_s, id_s = [], []
    for i, side in signals_with.items():
        s = 1 if side == "long" else -1
        on_s.append(s * (bars[i + 1].open / bars[i].close - 1))
        id_s.append(s * (bars[i + 1].close / bars[i + 1].open - 1))
    return {"overnight_after_strong_signed": stat(on_s), "intraday_after_strong_signed": stat(id_s),
            "overnight_all_days": stat(on_all), "intraday_all_days": stat(id_all)}


def _one_sided_p(t: float | None) -> float:
    return 1.0 if t is None else 0.5 * math.erfc(t / math.sqrt(2))


def block_p(trades: list[RTrade], draws: int = 1000, seed: int = 20261125) -> float:
    """One-sided p for mean net > 0 that respects overlap and cross-instrument clustering: trades are grouped by
    calendar quarter of entry, the groups are resampled with replacement around a mean-zero null."""
    if len(trades) < 2:
        return 1.0
    groups: dict[tuple[int, int], list[float]] = {}
    for t in trades:
        groups.setdefault((t.entry.year, (t.entry.month - 1) // 3), []).append(t.net)
    gl = list(groups.values())
    if len(gl) < 4:
        return 1.0
    allv = [x for g in gl for x in g]
    mu = statistics.fmean(allv)
    rng = random.Random(f"{seed}:{len(allv)}")
    hits = 0
    for _ in range(draws):
        pick = [gl[rng.randrange(len(gl))] for _ in gl]
        n = sum(len(g) for g in pick)
        m = (sum(sum(g) for g in pick) / n) - mu
        if m >= mu:
            hits += 1
    return (hits + 1) / (draws + 1)


def holm(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, float] = {}
    run = 0.0
    for rank, (k, p) in enumerate(items):
        run = max(run, min(1.0, (m - rank) * p))
        out[k] = run
    return out


def _year_share_positive(summary: dict[str, Any]) -> float | None:
    ys = [v for v in summary["per_year"].values() if v["n"] >= 3]
    return None if not ys else sum(1 for v in ys if v["total_eur"] > 0) / len(ys)


def run(bars_by_key: dict[str, list[technical.Bar]], instruments: dict[str, dict[str, Any]], risk: float = 5.0,
        implied_by_key: dict[str, dict[date, float]] | None = None, draws: int = 100, seed: int = 20261125) -> dict[str, Any]:
    """Every cell pooled and per instrument, the placebo (same exits, random dates and sides, same count of signals per
    instrument), the leg split after strong days and the verdict."""
    implied_by_key = {k: v for k, v in (implied_by_key or {}).items() if v} or None
    triggers = ["ewma"] + (["implied"] if implied_by_key else [])
    cells: dict[str, Any] = {}
    pvals: dict[str, float] = {}
    placebo_cache: dict[tuple[str, str], list[float]] = {}
    side_cache: dict[tuple[str, str], list[float]] = {}
    for trigger in triggers:
        for direction in DIRECTIONS:
            sigs = {k: strong_signals(b, direction, trigger, (implied_by_key or {}).get(k)) for k, b in bars_by_key.items()
                    if trigger == "ewma" or (implied_by_key or {}).get(k)}
            for exit_mode in EXITS:
                name = f"{trigger}:{direction}:{exit_mode}"
                per: dict[str, list[RTrade]] = {}
                gap_total = {"stops": 0, "gap_overruns": 0, "data_end": 0}
                est: list[RTrade] = []
                fresh: list[RTrade] = []
                for key, s in sigs.items():
                    trades, g = simulate_signals(key, bars_by_key[key], instruments[key], s, exit_mode, risk, tag=trigger)
                    per[key] = trades
                    for k2 in gap_total:
                        gap_total[k2] += g[k2]
                    by_date = {bars_by_key[key][i + 1].date: established(bars_by_key[key], i) for i in s if i + 1 < len(bars_by_key[key])}
                    for t in trades:
                        (est if by_date.get(t.entry) else fresh).append(t)
                allt = [t for ts in per.values() for t in ts]
                pooled = summarize(allt)
                pck = (trigger, exit_mode)
                if pck not in placebo_cache and draws > 0:
                    rng = random.Random(f"{seed}:{trigger}:{exit_mode}")
                    means: list[float] = []
                    for _ in range(draws):
                        tot: list[float] = []
                        for key, s in sigs.items():
                            n = len(s)
                            if not n:
                                continue
                            bars = bars_by_key[key]
                            idx = rng.sample(range(WARMUP, len(bars) - 1), min(n, len(bars) - 1 - WARMUP))
                            ps = {i: rng.choice(("long", "short")) for i in idx}
                            tr, _ = simulate_signals(key, bars, instruments[key], ps, exit_mode, risk, tag="placebo")
                            tot.extend(t.net for t in tr)
                        if tot:
                            means.append(statistics.fmean(tot))
                    placebo_cache[pck] = sorted(means)
                    srng = random.Random(f"{seed}:side:{trigger}:{exit_mode}")
                    smeans: list[float] = []
                    for _ in range(draws):
                        tot = []
                        for key, s in sigs.items():
                            ps = {i: srng.choice(("long", "short")) for i in s}
                            tr, _ = simulate_signals(key, bars_by_key[key], instruments[key], ps, exit_mode, risk, tag="side")
                            tot.extend(t.net for t in tr)
                        if tot:
                            smeans.append(statistics.fmean(tot))
                    side_cache[pck] = sorted(smeans)
                pm = placebo_cache.get(pck, [])
                real = pooled["mean_eur"]
                placebo = None
                if pm and real is not None:
                    placebo = {"draws": len(pm), "mean_eur": statistics.fmean(pm), "p05": pm[int(0.05 * (len(pm) - 1))],
                               "p95": pm[int(0.95 * (len(pm) - 1))], "p_value": sum(1 for x in pm if x >= real) / len(pm)}
                sm = side_cache.get(pck, [])
                side = None
                if sm and real is not None:
                    side = {"draws": len(sm), "mean_eur": statistics.fmean(sm), "p_value": sum(1 for x in sm if x >= real) / len(sm)}
                cells[name] = {"side_placebo": side, "pooled": pooled, "per_instrument": {k: summarize(v) for k, v in sorted(per.items())},
                               "established": summarize(est), "fresh": summarize(fresh), "gaps": gap_total,
                               "placebo": placebo, "year_share_positive": _year_share_positive(pooled)}
                cells[name]["t_p_descriptive"] = _one_sided_p(pooled["t"] if pooled["n"] else None)
                pvals[name] = block_p(allt)
    adj = holm(pvals)
    for name, c in cells.items():
        c["holm_p"] = adj[name]
        p = c["pooled"]
        ys = c["year_share_positive"]
        ok = (p["n"] >= MIN_TRADES and (p["mean_eur"] or 0) > 0 and adj[name] < 0.05
              and (c["side_placebo"] is not None and c["side_placebo"]["p_value"] < 0.05) and ys is not None and ys >= YEAR_POSITIVE_SHARE)
        c["verdict"] = "pass" if ok else "null"
    legs = {k: leg_stats(b, strong_signals(b, "with")) for k, b in bars_by_key.items()}
    return {"cells": cells, "legs": legs, "trials": len(cells),
            "note": "Research only; a pass would justify a new pre-registration, never a live signal."}
