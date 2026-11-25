"""Slow trend with a wide state exit on indices, commodities and FX daily bars (research only, paper only).

The pre-registered rule is ``lab/algo_lab/trend_cell`` (100-day breakout, 50-day EMA or 5 ATR exit, no clock). This
module only adds costs and the test, through the same engine as ``shock_ride_replay`` (spread, overnight financing per
calendar night held, 0.5 % FX on a USD result, risk-based sizing, gaps through a stop fill at the open, open trades at
the data end tagged ``data_end``).

Frozen 2026-11-25 before any real price was read. Trials declared: 3 families (indices, commodities, FX) x 2 exits
(``ema_cross``: EMA50 cross-back plus the initial 5 ATR stop; ``chandelier``: 5 ATR trailing stop only) = 6, Holm over
all six. Required control: the same breakout days with random sides (``side_placebo``), because a wide trailing exit
earns or loses with the market's own drift on any entry. p-values are the quarter-block bootstrap of
``shock_ride_replay.block_p``. Verdict ``pass`` needs pooled n >= ``MIN_TRADES``, mean net > 0, Holm p < 0.05, side
placebo p < 0.05 and at least 60 % of calendar years (3+ trades) positive; anything else is ``null``. Slow trend has
few trades per year, so a family with fewer than 100 trades is ``null`` by construction: say so rather than relax it.
A pass would justify a new pre-registration, never a live signal. Costs of commodities and FX are the assumptions in
``commodity_fx_replay``; commodity bars are unadjusted continuous futures (roll splices), so a commodity pass is
provisional until checked against roll dates.
"""
from __future__ import annotations

import random
import statistics
from typing import Any

from app.foundation import technical
from app.lab import shock_ride_replay as sr
from app.lab.algo_lab import trend_cell as tc
from app.lab.signal_replay import RTrade, summarize

EXITS = ("ema_cross", "chandelier")
MIN_TRADES = sr.MIN_TRADES
YEAR_POSITIVE_SHARE = sr.YEAR_POSITIVE_SHARE


def _simulate(key: str, bars: list[technical.Bar], inst: dict[str, Any], signals: dict[int, str], exit_mode: str,
              risk: float, tag: str) -> tuple[list[RTrade], dict[str, int]]:
    return sr.simulate_signals(key, bars, inst, signals, exit_mode, risk, tag=tag, stop_atr=tc.STOP_ATR, ema_n=tc.EMA_N,
                               atr_n=tc.ATR_N, ema_side="direction")


def run(families: dict[str, tuple[dict[str, list[technical.Bar]], dict[str, dict[str, Any]]]], risk: float = 5.0,
        draws: int = 100, seed: int = 20261125) -> dict[str, Any]:
    """``families``: name -> (bars by key, instrument cost dicts by key). Every cell pooled and per instrument."""
    cells: dict[str, Any] = {}
    pvals: dict[str, float] = {}
    for fam, (bars_by_key, insts) in families.items():
        sigs = {k: tc.breakout_signals([b.close for b in bars]) for k, bars in bars_by_key.items()}
        for exit_mode in EXITS:
            name = f"{fam}:{exit_mode}"
            per: dict[str, list[RTrade]] = {}
            gaps = {"stops": 0, "gap_overruns": 0, "data_end": 0}
            for key, s in sigs.items():
                trades, g = _simulate(key, bars_by_key[key], insts[key], s, exit_mode, risk, "trend")
                per[key] = trades
                for k2 in gaps:
                    gaps[k2] += g[k2]
            allt = [t for ts in per.values() for t in ts]
            pooled = summarize(allt)
            rng = random.Random(f"{seed}:{name}")
            means: list[float] = []
            for _ in range(draws):
                tot: list[float] = []
                for key, s in sigs.items():
                    ps = {i: rng.choice(("long", "short")) for i in s}
                    tr, _ = _simulate(key, bars_by_key[key], insts[key], ps, exit_mode, risk, "side")
                    tot.extend(t.net for t in tr)
                if tot:
                    means.append(statistics.fmean(tot))
            real = pooled["mean_eur"]
            side = None
            if means and real is not None:
                side = {"draws": len(means), "mean_eur": statistics.fmean(means),
                        "p_value": sum(1 for x in means if x >= real) / len(means)}
            cells[name] = {"pooled": pooled, "per_instrument": {k: summarize(v) for k, v in sorted(per.items())},
                           "side_placebo": side, "gaps": gaps, "year_share_positive": sr._year_share_positive(pooled)}
            pvals[name] = sr.block_p(allt)
    adj = sr.holm(pvals)
    for name, c in cells.items():
        c["holm_p"] = adj[name]
        p, ys = c["pooled"], c["year_share_positive"]
        ok = (p["n"] >= MIN_TRADES and (p["mean_eur"] or 0) > 0 and adj[name] < 0.05
              and c["side_placebo"] is not None and c["side_placebo"]["p_value"] < 0.05
              and ys is not None and ys >= YEAR_POSITIVE_SHARE)
        c["verdict"] = "pass" if ok else "null"
    return {"cell": tc.CELL_ID, "cells": cells, "trials": len(cells),
            "note": "Research only; a pass would justify a new pre-registration, never a live signal."}
