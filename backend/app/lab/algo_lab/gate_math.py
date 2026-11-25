"""How long a verdict takes, and whether an edge can pay its costs (research prototype).

Pure functions, annualised unless stated. Nothing here is a trading signal.

Sources: standard normal-approximation power for a Sharpe ratio (t-test on the mean of
i.i.d. returns); Jacquier, Muhle-Karbe, Mulligan (arXiv 2501.03938) for the univariate
in/out-of-sample Sharpe replication ratio; Sepp and Lucic (arXiv 2607.19497) for the
break-even cost idea (an edge is viable only if its gross edge exceeds cost x turnover).
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()


def years_to_detect(
    sharpe: float, alpha: float = 0.05, power: float = 0.80, *, trials: int = 1, alt_se: bool = False
) -> float:
    """Years of daily data for a one-sided test to detect annual ``sharpe`` at ``power``.

    ``trials`` applies a Bonferroni split of ``alpha`` (a conservative stand-in for Holm).
    The i.i.d. approximation understates the time when returns are fat-tailed or serially
    dependent. ``alt_se=True`` uses the larger standard error of the Sharpe estimate under
    the alternative, ``sqrt((1 + SR_p^2 / 2) / T)`` (Lo 2002, Gaussian i.i.d.) with the per-period Sharpe
    ``SR_p = SR / sqrt(252)``: well under 1 % longer at daily frequency.
    """
    if sharpe <= 0:
        return math.inf
    z_a = _N.inv_cdf(1 - alpha / max(1, trials))
    z_b = _N.inv_cdf(power)
    infl = math.sqrt(1 + sharpe**2 / (2 * 252)) if alt_se else 1.0
    return ((z_a + z_b * infl) / sharpe) ** 2


def minimum_detectable_sharpe(years: float, alpha: float = 0.05, power: float = 0.80, *, trials: int = 1) -> float:
    """The smallest annual Sharpe a one-sided test can detect in ``years`` at ``power``."""
    if years <= 0:
        return math.inf
    z_a = _N.inv_cdf(1 - alpha / max(1, trials))
    z_b = _N.inv_cdf(power)
    return (z_a + z_b) / math.sqrt(years)


def holm_alpha(alpha: float, m: int) -> float:
    """The first (strictest) Holm step: ``alpha / m``."""
    return alpha / max(1, m)


def oos_replication_ratio(true_sharpe_annual: float, train_years: float, periods_per_year: int = 252) -> float:
    """Expected out-of-sample / in-sample Sharpe for ONE signal, ONE asset, fitted by OLS.

    Jacquier et al. (2025) eq. 3.3 with iid signals. A guide to the haircut for a fitted
    model with a single free coefficient; more signals or assets give a lower ratio, so
    treat the result as an upper bound. Returns a value in (0, 1).
    """
    s = true_sharpe_annual / math.sqrt(periods_per_year)
    if s <= 0 or 2 * s * s >= 1:
        return 0.0
    t1 = train_years * periods_per_year
    if t1 <= 5:
        return 0.0
    b2 = s * s / (1 - 2 * s * s)  # beta squared, so that SR = b2 / sqrt(2 b2^2 + b2)
    is_den = 2 * b2**2 + (1 + 15 / (t1 - 2) - 2 / t1) * b2 + 4 / t1 - 3 / (t1 + 2) - 1 / t1**2
    oos_den = 2 * b2**2 + (1 + 2 / (t1 - 2)) * b2 + 1 / (t1 - 2)
    sr_is = (b2 + 1 / t1) / math.sqrt(is_den)
    sr_oos = b2 / math.sqrt(oos_den)
    return max(0.0, min(1.0, sr_oos / sr_is))


def breakeven_cost_bps(gross_sharpe: float, vol_annual: float, turnover_annual: float) -> float:
    """One-way cost in bp per unit of notional traded at which net expected return is zero.

    ``vol_annual`` and ``turnover_annual`` are fractions of the position (turnover 12 = the
    whole position traded 12 times a year, one way). Gross return = Sharpe x vol.
    """
    if turnover_annual <= 0:
        return math.inf
    return 1e4 * gross_sharpe * vol_annual / turnover_annual


def net_sharpe(gross_sharpe: float, vol_annual: float, turnover_annual: float, cost_bps: float) -> float:
    """Gross Sharpe less the annual cost drag expressed in Sharpe units."""
    if vol_annual <= 0:
        return 0.0
    return gross_sharpe - (cost_bps / 1e4) * turnover_annual / vol_annual


def per_order_cost_eur(notional_eur: float, pct: float, minimum_eur: float, maximum_eur: float | None = None) -> float:
    """Commission for one order: ``pct`` of notional, floored at ``minimum_eur`` and optionally capped."""
    fee = max(notional_eur * pct, minimum_eur)
    return fee if maximum_eur is None else min(fee, maximum_eur)


def prob_drawdown_breach(
    mu_annual: float,
    sigma_annual: float,
    years: float,
    drawdown_limit: float,
    *,
    n_paths: int = 20_000,
    seed: int = 20261125,
    periods_per_year: int = 252,
) -> float:
    """Chance that a geometric-Brownian equity curve falls ``drawdown_limit`` (e.g. 0.2) below its peak.

    Used to size a kill-switch before it is needed: with zero edge, the chance of a 20 %
    drawdown over a few years is high at moderate volatility, so a halt level is a
    cost, not only protection. Daily Gaussian steps; fat tails make the real chance higher.
    """
    n = max(1, int(years * periods_per_year))
    dt = 1.0 / periods_per_year
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n_paths, n))
    log_ret = (mu_annual - 0.5 * sigma_annual**2) * dt + sigma_annual * math.sqrt(dt) * z
    equity = np.exp(np.cumsum(log_ret, axis=1))
    peak = np.maximum.accumulate(np.concatenate([np.ones((n_paths, 1)), equity], axis=1), axis=1)[:, 1:]
    dd = 1.0 - equity / peak
    return float(np.mean(dd.max(axis=1) >= drawdown_limit))
