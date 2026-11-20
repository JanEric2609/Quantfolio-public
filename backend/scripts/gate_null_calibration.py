"""Null calibration and power for the statistical gates (plan 2026-11-12, WP15 items 1 and 2).

For each gate: how often does it fire when there is NO effect (stated against measured), and how large must a
real effect be before it fires with 80 % power at the present sample. Nothing here is a strategy trial; do not
write it to the trial ledger.

    python scripts/gate_null_calibration.py [--sims 2000] [--seed 1] > table.md
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.decision.graduation.ladder import e_processes, lagged_sigma  # noqa: E402
from app.foundation.capital_ladder import EARNED_ALPHA, HARM_THRESHOLD, PROVEN_ALPHA, thresholds  # noqa: E402
from app.foundation.quant_metrics import deflated_ic_sharpe, newey_west_t_stat  # noqa: E402

DAYS = 252


def draws(rng: np.random.Generator, kind: str, n: int, mu: float = 0.0) -> np.ndarray:
    """Daily paired excess in units of its own sd: normal, fat-tailed (t3) or volatility-clustered."""
    if kind == "normal":
        return mu + rng.standard_normal(n)
    if kind == "t3":
        return mu + rng.standard_t(3, n) / math.sqrt(3.0)
    vol = np.exp(0.5 * np.cumsum(0.1 * rng.standard_normal(n)) - 0.1 * np.arange(n) / 50)  # clustered, drifting vol
    return mu * vol + vol * rng.standard_normal(n)


def ladder_rates(kind: str, sims: int, rng: np.random.Generator, days: int, mu: float, pi: float = 0.5) -> dict[str, float]:
    earned, proven = 1.0 / (EARNED_ALPHA * pi), 1.0 / (PROVEN_ALPHA * pi)
    hit_e = hit_p = hit_h = 0
    for _ in range(sims):
        x = draws(rng, kind, days, mu)
        sk, hm = e_processes(x, lagged_sigma(x))
        hit_e += bool(sk.max() >= earned)
        hit_p += bool(sk.max() >= proven)
        hit_h += bool(hm.max() >= HARM_THRESHOLD)
    return {"earned": hit_e / sims, "proven": hit_p / sims, "harm": hit_h / sims}


def nw_rate(sims: int, rng: np.random.Generator, n: int, rho: float, t_cut: float, icir: float = 0.0) -> float:
    hits = 0
    for _ in range(sims):
        e = rng.standard_normal(n)
        x = np.empty(n)
        x[0] = e[0]
        for i in range(1, n):
            x[i] = rho * x[i - 1] + math.sqrt(1 - rho**2) * e[i]
        hits += newey_west_t_stat(x * 0.05 + icir * 0.05, lags=2) >= t_cut
    return hits / sims


def dsr_rate(sims: int, rng: np.random.Generator, n: int, trials: int) -> float:
    return sum(deflated_ic_sharpe(rng.standard_normal(n) * 0.05, trials) > 0.95 for _ in range(sims)) / sims


def power_ladder(kind: str, sims: int, rng: np.random.Generator, days: int) -> float | None:
    """Smallest daily mean (in sd units) that reaches the Earned bar within ``days`` in 80 % of runs."""
    for mu in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15, 0.2, 0.3, 0.5):
        if ladder_rates(kind, max(sims // 4, 100), rng, days, mu)["earned"] >= 0.8:
            return mu
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    s = a.sims
    print(f"# Gate null calibration and power ({s} simulations per cell, seed {a.seed})\n")
    print(f"## Capital ladder e-processes, {DAYS} trading days, no effect\n")
    print("| Daily excess | P(Earned bar ever) | stated <= | P(Proven bar ever) | stated <= | P(harm >= 10 ever) | stated <= |")
    print("|---|---|---|---|---|---|---|")
    e_b, p_b = thresholds("rules", 1)
    for kind in ("normal", "t3", "clustered"):
        r = ladder_rates(kind, s, rng, DAYS, 0.0)
        print(f"| {kind} | {r['earned']:.4f} | {1 / e_b:.4f} | {r['proven']:.4f} | {1 / p_b:.4f} | {r['harm']:.4f} | {1 / HARM_THRESHOLD:.4f} |")
    print("\n## Ladder power: smallest daily mean/sd (grid-resolved) that reaches the Earned bar within the horizon in 80 % of runs\n")
    print("| Daily excess | horizon (trading days) | mean/sd per day | annualised IR |\n|---|---|---|---|")
    for kind in ("normal", "t3"):
        for days in (252, 756, 1260):
            mu = power_ladder(kind, s, rng, days)
            print(f"| {kind} | {days} | {mu if mu is not None else '> 0.5'} | {'' if mu is None else f'{mu * math.sqrt(252):.2f}'} |")
    print("\n## IC gates, no effect\n")
    print("| Gate | n | autocorr | measured | stated |\n|---|---|---|---|---|")
    for n in (42, 120):
        for rho in (0.0, 0.3):
            print(f"| Newey-West t >= 2 (one-sided) | {n} | {rho} | {nw_rate(s, rng, n, rho, 2.0):.4f} | 0.0228 |")
            print(f"| Newey-West t >= 3 (one-sided) | {n} | {rho} | {nw_rate(s, rng, n, rho, 3.0):.4f} | 0.0013 |")
    for trials in (1, 10, 30):
        print(f"| Deflated IC-Sharpe > 0.95, N={trials} | 42 | 0 | {dsr_rate(s, rng, 42, trials):.4f} | 0.0500 |")
    print("\n## IC gate power (Newey-West t >= 3, 2 lags): ICIR per period needed for 80 % power\n")
    print("| n periods | needed ICIR (analytic, t=3+0.84) | measured power at that ICIR (rho 0) |\n|---|---|---|")
    for n in (42, 120, 250):
        icir = (3.0 + 0.84) / math.sqrt(n)
        print(f"| {n} | {icir:.2f} | {nw_rate(max(s // 2, 200), rng, n, 0.0, 3.0, icir=icir):.2f} |")


if __name__ == "__main__":
    main()
