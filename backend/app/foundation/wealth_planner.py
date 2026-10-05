"""Monte Carlo projection of the book in real EUR, with monthly contributions.

The old Monte Carlo tab simulated one asset from S0 = 100 at a hand-typed 5 %
drift and showed Greeks: a derivatives pricer, not a plan. This projects the
book itself, the way the research for it recommends (2026-10-04 review):

* **Real terms.** Every amount is in today's euros; the contribution stays
  constant in real terms.
* **Log space, median first.** Monthly log returns with drift ``m = ln(1+g)``,
  where ``g`` is a geometric (compound) real return. The median path grows
  at ``g``; the mean is higher by ``exp(sigma^2/2)`` a year and is shown only
  for contrast. Plugging an arithmetic mean in as the log drift would
  overstate the median by about ``sigma^2/2`` a year.
* **Drift from a capital-market assumption, not from history.** Two to ten
  years of returns pin the mean down to within +/- 10 % a year (standard
  error ``sigma/sqrt(T)``), so ``g`` is a published long-run estimate stored
  as a setting with its source and date (default: AQR Capital Market
  Assumptions 2026, global developed equities, 4.2 % real compound).
* **Drift uncertainty.** Each path draws its own drift
  ``m + sigma/sqrt(T_cal) * eps`` (Zumbach 2025, eq. 14), so the fan widens
  the way it should over decades; P(goal) is also shown at ``g`` -/+ one
  standard error.
* **Fat tails.** Monthly shocks are Student-t with ``nu`` degrees of freedom
  scaled by ``sqrt((nu - 2)/nu)`` to unit variance, so ``sigma`` keeps its
  meaning.

The book is simulated as one constant-mix asset with the volatility of its
current weights (Ledoit-Wolf covariance of EUR returns), which is exact for a
portfolio rebalanced to fixed weights each month.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

# AQR Alternative Thinking 2026 Issue 1, Exhibit A1: Global Dev. equities,
# local real compound return, estimates as of 2025-12-31.
DEFAULT_CMA_REAL_RETURN = 0.042
DEFAULT_CMA_SOURCE = "AQR Capital Market Assumptions 2026, global developed equities, real compound return"
DEFAULT_CMA_AS_OF = "2025-12-31"
# Long-run volatility of MSCI World in EUR, used until the book has a year of history.
DEFAULT_VOLATILITY = 0.16
DEFAULT_NU = 5.0
DEFAULT_CALIBRATION_YEARS = 25.0
PERCENTILES = (5, 25, 50, 75, 95)


@dataclass(frozen=True)
class PlanInputs:
    start_value: float
    monthly_contribution: float
    years: int
    real_return: float  # geometric, real, a year
    volatility: float  # a year
    goal: float | None = None
    nu: float = DEFAULT_NU
    calibration_years: float = DEFAULT_CALIBRATION_YEARS
    paths: int = 10_000
    seed: int = 7


def _shocks(rng: np.random.Generator, size: tuple[int, int], nu: float) -> np.ndarray:
    if nu is None or nu <= 2 or not math.isfinite(nu):
        return rng.standard_normal(size)
    return rng.standard_t(nu, size=size) * math.sqrt((nu - 2.0) / nu)


def _simulate(p: PlanInputs, drift_shift: float, drift_noise: bool, z: np.ndarray, e: np.ndarray) -> np.ndarray:
    """Wealth at each month end (paths x months+1), contributions at the start of each month."""
    months = p.years * 12
    sigma_m = p.volatility / math.sqrt(12.0)
    m = math.log1p(p.real_return) + drift_shift
    sigma_mu = p.volatility / math.sqrt(p.calibration_years) if p.calibration_years > 0 else 0.0
    drift = m + (sigma_mu * e if drift_noise else 0.0)  # per path, a year
    log_r = (np.asarray(drift).reshape(-1, 1) / 12.0) + sigma_m * z[:, :months]
    growth = np.exp(log_r)
    wealth = np.empty((p.paths, months + 1))
    wealth[:, 0] = p.start_value
    w = np.full(p.paths, p.start_value, dtype=float)
    for t in range(months):
        w = (w + p.monthly_contribution) * growth[:, t]
        wealth[:, t + 1] = w
    return wealth


def project(p: PlanInputs) -> dict[str, Any]:
    """Fan of real wealth by year, P(goal) with its Monte Carlo SE and drift sensitivity."""
    months = p.years * 12
    rng = np.random.default_rng(p.seed)
    z = _shocks(rng, (p.paths, months), p.nu)
    e = rng.standard_normal(p.paths)
    wealth = _simulate(p, 0.0, True, z, e)
    year_idx = [12 * y for y in range(p.years + 1)]
    fan = []
    for y, i in zip(range(p.years + 1), year_idx):
        q = np.percentile(wealth[:, i], PERCENTILES)
        fan.append({"year": y, **{f"p{pc}": float(v) for pc, v in zip(PERCENTILES, q)},
                    "contributed": p.start_value + p.monthly_contribution * 12 * y})
    terminal = wealth[:, -1]
    sigma_mu = p.volatility / math.sqrt(p.calibration_years) if p.calibration_years > 0 else 0.0
    out: dict[str, Any] = {
        "inputs": {
            "start_value_eur": p.start_value, "monthly_contribution_eur": p.monthly_contribution,
            "years": p.years, "real_return": p.real_return, "volatility": p.volatility, "nu": p.nu,
            "calibration_years": p.calibration_years, "drift_standard_error": sigma_mu, "paths": p.paths,
            "seed": p.seed, "goal_eur": p.goal,
        },
        "fan": fan,
        "terminal": {
            "median": float(np.median(terminal)),
            "mean": float(np.mean(terminal)),
            "p5": float(np.percentile(terminal, 5)),
            "p95": float(np.percentile(terminal, 95)),
            "contributed": p.start_value + p.monthly_contribution * months,
        },
        "real_terms": True,
        "estimate": True,
    }
    if p.goal is not None and p.goal > 0:
        prob = float(np.mean(terminal >= p.goal))
        goal: dict[str, Any] = {
            "goal_eur": p.goal,
            "probability": prob,
            "mc_standard_error": math.sqrt(prob * (1.0 - prob) / p.paths),
        }
        # Common random numbers: the same shocks with the drift fixed at g -/+ one SE.
        for name, shift in (("probability_low_drift", -sigma_mu), ("probability_high_drift", sigma_mu)):
            alt = _simulate(p, shift, False, z, e)
            goal[name] = float(np.mean(alt[:, -1] >= p.goal))
        median_path = np.median(wealth, axis=0)
        reach = int(np.argmax(median_path >= p.goal)) if (median_path >= p.goal).any() else None
        goal["median_reaches_in_years"] = None if reach is None else reach / 12.0
        out["goal"] = goal
    return out
