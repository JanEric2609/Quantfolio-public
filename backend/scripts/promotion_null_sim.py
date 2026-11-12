"""Null simulation of the advisor's promotion decision (plan 2026-11-12, WP10 item 1).

Generates matched forecast-loss differences with NO true difference and the real overlap structure (a
21-trading-day forecast horizon makes consecutive differences a moving average of 21 independent shocks), then
runs the promotion test the way ``decision/advisor/promotion.py`` does: up to six looks, 21 trading days apart,
the first once 60 trading days are in, each read against the Pocock nominal level. Reports, per look and over
all looks, how often a null challenger would be promoted, with a Monte Carlo interval.

    python scripts/promotion_null_sim.py [--sims 4000] [--seed 1] [--estimator hln|cosine|every21]
"""
from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.decision.advisor.promotion import (  # noqa: E402
    HORIZON_DAYS,
    LOOK_SPACING_TRADING_DAYS,
    MAX_LOOKS,
    MIN_TRADING_DAYS,
    NW_LAG,
    POCOCK_NOMINAL_ALPHA,
)
from app.foundation.quant_metrics import diebold_mariano_hln  # noqa: E402

Test = Callable[[np.ndarray], float | None]


def p_hln(d: np.ndarray) -> float | None:
    """What the advisor runs today: Newey-West at 20 lags, HLN factor, t(T-1)."""
    out = diebold_mariano_hln(d.tolist(), h=HORIZON_DAYS, lag=NW_LAG, positions=list(range(len(d))))
    return out["p_value"]


def p_every21(d: np.ndarray) -> float | None:
    """One-sample t test on every 21st date: the non-overlapping fallback."""
    from scipy.stats import t as student

    x = d[::HORIZON_DAYS]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return None
    return float(student.sf(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))), len(x) - 1))


def p_cosine(d: np.ndarray) -> float | None:
    """Equal-weighted cosine variance with nu = floor(0.4 T^(2/3)), read against t(nu) (Lazarus et al. 2018)."""
    from scipy.stats import t as student

    t = len(d)
    nu = int(math.floor(0.4 * t ** (2.0 / 3.0)))
    if nu < 1 or t < 3:
        return None
    idx = np.arange(1, t + 1)
    lam = np.array([math.sqrt(2.0 / t) * float(np.sum(np.cos(math.pi * j * (idx - 0.5) / t) * d)) for j in range(1, nu + 1)])
    sigma2 = float(np.mean(lam**2))
    if sigma2 <= 0:
        return None
    return float(student.sf(math.sqrt(t) * d.mean() / math.sqrt(sigma2), nu))


ESTIMATORS: dict[str, Test] = {"hln": p_hln, "cosine": p_cosine, "every21": p_every21}


def null_differences(rng: np.random.Generator, days: int) -> np.ndarray:
    """Overlapping differences with zero mean: a 21-day moving average of independent shocks."""
    shocks = rng.standard_normal(days + HORIZON_DAYS - 1)
    return np.convolve(shocks, np.ones(HORIZON_DAYS) / HORIZON_DAYS, mode="valid")


def simulate(estimator: str, sims: int, seed: int) -> dict[str, object]:
    test = ESTIMATORS[estimator]
    rng = np.random.default_rng(seed)
    total_days = MIN_TRADING_DAYS + (MAX_LOOKS - 1) * LOOK_SPACING_TRADING_DAYS
    per_look = np.zeros(MAX_LOOKS)
    ever = 0
    for _ in range(sims):
        d = null_differences(rng, total_days)
        promoted = False
        for look in range(MAX_LOOKS):
            p = test(d[: MIN_TRADING_DAYS + look * LOOK_SPACING_TRADING_DAYS])
            if p is not None and p < POCOCK_NOMINAL_ALPHA:
                per_look[look] += 1
                promoted = True
                break                       # the advisor stops at the first promotion
        ever += promoted
    rate = ever / sims
    half = 1.96 * math.sqrt(rate * (1 - rate) / sims)
    return {"estimator": estimator, "sims": sims, "overall_rate": rate, "ci95": (max(0.0, rate - half), rate + half),
            "first_promotion_by_look": (per_look / sims).round(5).tolist(),
            "stated_overall": 0.05 / 4, "stated_per_look": POCOCK_NOMINAL_ALPHA}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sims", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--estimator", choices=sorted(ESTIMATORS), default="hln")
    args = parser.parse_args()
    for key, value in simulate(args.estimator, args.sims, args.seed).items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
