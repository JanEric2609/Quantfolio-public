"""Per-cell scorecard: bootstrap, placebo, Holm and the pass flag (ADR 0027, plan section 6)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskPosition

from . import prereg

BOOTSTRAP_DRAWS = prereg.SPEC["bootstrap"]["draws"]
BOOTSTRAP_SEED = prereg.SPEC["bootstrap"]["seed"]
PLACEBO_DRAWS = prereg.SPEC["placebo"]["draws"]
PLACEBO_SEED = prereg.SPEC["placebo"]["seed"]
N_MIN = prereg.SPEC["n_min"]
ALPHA = 0.05
#: Share of a cell's trades that may lack the underlying move (``prereg.SPEC["placebo"]["missing_move"]``).
MAX_MISSING_MOVE = 0.10


@dataclass(frozen=True)
class Trade:
    day: str
    net: float
    notional: float
    leverage: float
    move: float | None  # underlying move signed in the taken direction; None when it was not recorded
    cost: float  # fees plus the spread paid, EUR
    would_take_live: bool = False
    liquid: bool = False


def _bootstrap_p(trades: list[Trade]) -> float:
    """One-sided p that mean net <= 0: resample trading days with replacement."""
    days: dict[str, list[float]] = {}
    for t in trades:
        days.setdefault(t.day, []).append(t.net)
    groups = list(days.values())
    sums = np.array([sum(g) for g in groups])
    counts = np.array([len(g) for g in groups])
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    idx = rng.integers(0, len(groups), size=(BOOTSTRAP_DRAWS, len(groups)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    return float((means <= 0).mean())


def _placebo_means(trades: list[Trade]) -> np.ndarray:
    real = np.array([t.net for t in trades])
    flipped = np.array([-t.leverage * (t.move or 0.0) * t.notional - t.cost for t in trades])
    rng = np.random.default_rng(PLACEBO_SEED)
    keep = rng.random((PLACEBO_DRAWS, len(trades))) < 0.5
    return np.where(keep, real, flipped).mean(axis=1)


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    order = sorted(pvalues, key=lambda k: pvalues[k])
    m, running, out = len(order), 0.0, {}
    for i, key in enumerate(order):
        running = max(running, min(1.0, (m - i) * pvalues[key]))
        out[key] = running
    return out


def score_cell(trades: list[Trade]) -> dict[str, Any]:
    n = len(trades)
    base: dict[str, Any] = {"n": n, "n_min": N_MIN, "mean_net": None, "hit_rate": None, "median_cost_share": None,
                            "p": 1.0, "placebo_percentile": None, "placebo_p95": None}
    if n == 0:
        return base
    nets = [t.net for t in trades]
    base.update(
        mean_net=statistics.fmean(nets), hit_rate=sum(1 for x in nets if x > 0) / n,
        median_cost_share=statistics.median(t.cost / t.notional for t in trades if t.notional > 0) if any(
            t.notional > 0 for t in trades) else None,
        total_net=sum(nets),
    )
    base["missing_move"] = sum(1 for t in trades if t.move is None)
    if n >= N_MIN:
        base["p"] = _bootstrap_p(trades)
        if base["missing_move"] > MAX_MISSING_MOVE * n:
            # Too many trades without the underlying move: no placebo, so the cell cannot pass.
            return base
        # Without the move the opposite-direction result would be just -cost, an easy bar: those trades
        # are left out of the placebo and of the mean it is compared with (pre-registered, "missing_move").
        known = [t for t in trades if t.move is not None]
        means = _placebo_means(known)
        base["placebo_mean_net"] = statistics.fmean(t.net for t in known)
        base["placebo_p95"] = float(np.percentile(means, 95))
        base["placebo_percentile"] = float((means < base["placebo_mean_net"]).mean() * 100)
    return base


def _trade(p: DeskPosition) -> Trade | None:
    size = p.units if p.units is not None else p.quantity  # a CFD has a fractional quantity
    if p.net_eur is None or p.entry_at is None or size is None or p.entry_price is None:
        return None
    notional = float(p.entry_price) * float(size)
    spread_cost = float(p.spread_pct or 0) * notional
    cost = float((p.entry_fee or 0) + (p.exit_fee or 0)) + spread_cost
    return Trade(
        day=p.entry_at.date().isoformat(), net=float(p.net_eur), notional=notional,
        leverage=float(p.leverage_or_omega or 0),
        move=None if p.underlying_move is None else float(p.underlying_move), cost=cost,
        would_take_live=bool(p.would_take_live), liquid=bool(p.liquid),
    )


def scorecard(db: Session) -> dict[str, Any]:
    reg_at = prereg.registered_at(db)
    cells: dict[str, list[Trade]] = {c: [] for c in prereg.CELLS}
    external = 0
    if reg_at is not None:
        rows = db.query(DeskPosition).filter(DeskPosition.mode == "paper", DeskPosition.status == "closed").all()
        for p in rows:
            entry = p.entry_at if p.entry_at is None or p.entry_at.tzinfo else p.entry_at.replace(tzinfo=UTC)
            if entry is None or entry <= reg_at:
                continue
            if p.signal == "external":
                external += 1
            elif p.cell in cells and (t := _trade(p)):
                cells[p.cell].append(t)
    scored = {c: score_cell(t) for c, t in cells.items()}
    adjusted = holm({c: s["p"] for c, s in scored.items()})
    for c, s in scored.items():
        s["p_holm"] = adjusted[c]
        mean = s["mean_net"]
        s["passes"] = bool(
            s["n"] >= N_MIN and mean is not None and mean > 0 and adjusted[c] < ALPHA
            and s["placebo_p95"] is not None and s["placebo_mean_net"] > s["placebo_p95"]
        )
        trades = cells[c]
        s["split"] = {
            "liquid": _split(trades, lambda t: t.liquid), "other": _split(trades, lambda t: not t.liquid),
            "would_take_live": _split(trades, lambda t: t.would_take_live),
        }
    return {
        "prereg_hash": prereg.PREREG_HASH, "registered_at": reg_at.isoformat() if reg_at else None,
        "n_min": N_MIN, "cells": scored, "external_closed": external,
        "estimate": True, "not_tax_advice": True,
    }


def _split(trades: list[Trade], pick) -> dict[str, Any]:
    sel = [t for t in trades if pick(t)]
    return {"n": len(sel), "mean_net": statistics.fmean(t.net for t in sel) if sel else None}

