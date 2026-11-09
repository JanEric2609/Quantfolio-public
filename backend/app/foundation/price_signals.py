"""Price-only stock signals shared by Discover and the forward-test lab.

Each takes EUR closes, oldest first, and returns ``None`` when the history is
too short. :func:`residual_momentum` and :func:`welch_beta` moved here from
``decision/discover/pipeline.py`` unchanged, so the nightly lab
(``lab/forward_lab``) measures exactly what Discover records.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypeAlias
from typing import Any

import numpy as np
import pandas as pd

#: Months of returns behind the residual-momentum regression (Blitz, Huij & Martens 2011: 36).
RESIDUAL_MOMENTUM_MIN_MONTHS = 24
#: Trading days in the 12-1 formation window and the skipped month.
YEAR_DAYS = 252
SKIP_DAYS = 21

#: Closes, oldest first: a list or a NumPy array.
Closes: TypeAlias = Sequence[float] | np.ndarray


def residual_momentum(dates: Any, cand_closes: Any, bench_closes: Any) -> float | None:
    """Residual momentum (Blitz, Huij & Martens 2011), against the candidate's benchmark.

    Month-end returns of both legs; the candidate's regressed on the
    benchmark's over every month but the latest; the residuals of the eleven
    months t-12..t-2 summed and divided by their standard deviation. Ranking on
    what the market does not explain reduced momentum's crashes and about doubled
    its risk-adjusted return in their 1926-2009 sample. CAPM residuals here: the
    paper uses Fama-French factors, which are not stored in EUR.
    """
    frame = pd.DataFrame(
        {"c": list(cand_closes), "b": list(bench_closes)}, index=pd.to_datetime(pd.Series(list(dates)))
    ).astype(float)
    monthly = frame.resample("ME").last().pct_change().dropna()
    # The latest month is skipped, as in 12-1 momentum.
    estimation = monthly.iloc[:-1]
    if len(estimation) < RESIDUAL_MOMENTUM_MIN_MONTHS:
        return None
    x = estimation["b"].to_numpy()
    y = estimation["c"].to_numpy()
    x_dev = x - x.mean()
    denom = float((x_dev**2).sum())
    if denom <= 0:
        return None
    slope = float((x_dev * (y - y.mean())).sum()) / denom
    residuals = y - (y.mean() + slope * x_dev)
    formation = residuals[-11:]
    spread = float(formation.std(ddof=1))
    if not math.isfinite(spread) or spread <= 0:
        return None
    return float(formation.sum()) / spread


def welch_beta(cand_returns: list[float], bench_returns: list[float]) -> float | None:
    """Slope-winsorized market beta (Welch 2022, "Simply Better Market Betas").

    Each day's stock return is clipped to the band between -2 and +4 times that
    day's market return, then an ordinary least-squares slope is fitted. It
    forecast next year's beta better than OLS, Vasicek shrinkage and the
    Blume adjustment in his tests, because one-day jumps no longer pull the slope.
    """
    if len(cand_returns) != len(bench_returns) or len(cand_returns) < 60:
        return None
    rc = pd.Series(cand_returns, dtype=float)
    rm = pd.Series(bench_returns, dtype=float)
    lo = pd.concat([-2.0 * rm, 4.0 * rm], axis=1).min(axis=1)
    hi = pd.concat([-2.0 * rm, 4.0 * rm], axis=1).max(axis=1)
    clipped = rc.clip(lower=lo, upper=hi)
    var = float(rm.var(ddof=1))
    if not math.isfinite(var) or var <= 0:
        return None
    return float(clipped.cov(rm)) / var


def momentum_12_1(closes: Closes) -> float | None:
    """Return from 252 to 21 trading days ago (Jegadeesh & Titman), as Discover ranks it."""
    c = np.asarray(closes, dtype=float)
    if len(c) < YEAR_DAYS or c[-YEAR_DAYS] <= 0:
        return None
    return float(c[-SKIP_DAYS] / c[-YEAR_DAYS] - 1.0)


def formation_returns(closes: Closes) -> np.ndarray:
    """Daily returns inside the 12-1 window, as ``pipeline._largest_day_in_formation`` takes them."""
    c = np.asarray(closes, dtype=float)
    start = len(c) - YEAR_DAYS if len(c) >= YEAR_DAYS else 0
    end = len(c) - SKIP_DAYS if len(c) >= SKIP_DAYS else len(c) - 1
    window = c[start : end + 1]
    return window[1:] / window[:-1] - 1.0


def information_discreteness(closes: Closes) -> float | None:
    """Da, Gurun & Warachka (2014): sign(momentum) x (share of falling days - share of rising days).

    Near -1 is a steady climb ("frog in the pan"), which continued better than
    momentum that came in a few jumps.
    """
    mom = momentum_12_1(closes)
    rets = formation_returns(closes)
    if mom is None or len(rets) == 0:
        return None
    return math.copysign(1.0, mom) * (float((rets < 0).mean()) - float((rets > 0).mean()))


def momentum_without_largest_day(closes: Closes) -> float | None:
    """12-1 momentum with the window's largest one-day gain taken out (Discover's rank input)."""
    mom = momentum_12_1(closes)
    rets = formation_returns(closes)
    if mom is None or len(rets) == 0:
        return None
    largest = float(rets.max())
    if mom > 0 and largest > 0:
        return (1.0 + mom) / (1.0 + largest) - 1.0
    return mom


def high_52w_ratio(closes: Closes) -> float | None:
    """Latest close over the 52-week high (George & Hwang 2004)."""
    c = np.asarray(closes, dtype=float)
    if len(c) < YEAR_DAYS:
        return None
    high = float(c[-YEAR_DAYS:].max())
    return float(c[-1]) / high if high > 0 else None


def volatility(closes: Closes, days: int = 126) -> float | None:
    """Annualised standard deviation of the last *days* daily returns."""
    c = np.asarray(closes, dtype=float)
    if len(c) < days + 1:
        return None
    r = c[-days:] / c[-days - 1 : -1] - 1.0
    return float(r.std(ddof=1) * math.sqrt(YEAR_DAYS))


def return_over(closes: Closes, days: int = SKIP_DAYS) -> float | None:
    """Return over the last *days* trading days."""
    c = np.asarray(closes, dtype=float)
    if len(c) < days + 1 or c[-days - 1] <= 0:
        return None
    return float(c[-1] / c[-days - 1] - 1.0)


__all__ = [
    "RESIDUAL_MOMENTUM_MIN_MONTHS",
    "formation_returns",
    "high_52w_ratio",
    "information_discreteness",
    "momentum_12_1",
    "momentum_without_largest_day",
    "residual_momentum",
    "return_over",
    "volatility",
    "welch_beta",
]
