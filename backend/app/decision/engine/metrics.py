"""Pure mathematical metrics engine: zero-dependency calculations for portfolio performance.

This module belongs to ``app.decision.engine`` and must have ZERO dependencies on ``app.services``.
All functions accept generic sequences / numbers and return pure Python floats/dicts.
"""
from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Sequence

import numpy as np
from scipy.stats import kurtosis as _scipy_kurtosis
from scipy.stats import skew as _scipy_skew


def _finite_or_zero(value: float) -> float:
    """Degenerate inputs produce NaN/Inf; return 0.0 instead."""
    v = float(value)
    return v if math.isfinite(v) else 0.0


def _to_list(values: Sequence[float]) -> list[float]:
    """Convert a sequence to a list of floats, excluding None values."""
    return [float(v) for v in values if v is not None]


def annualised_return(returns: Sequence[float], periods_per_year: int = 252) -> float:
    """Geometric compounding: prod(1+r)^(periods_per_year/N) - 1."""
    rs = _to_list(returns)
    n = len(rs)
    if n == 0:
        return 0.0
    if any(1.0 + r <= 0.0 for r in rs):
        return 0.0
    log_sum = sum(math.log(1.0 + r) for r in rs)
    return math.exp(log_sum * periods_per_year / n) - 1.0


def annualised_volatility(returns: Sequence[float], periods_per_year: int = 252) -> float:
    rs = _to_list(returns)
    if len(rs) < 2 or stdev(rs) == 0:
        return 0.0
    return _finite_or_zero(float(np.std(rs, ddof=1)) * math.sqrt(periods_per_year))


def downside_deviation(returns: Sequence[float], target: float = 0.0) -> float:
    """Sample semi-deviation: sqrt( sum((r-target)^2 for r<target) / (N-1) )."""
    rs = _to_list(returns)
    n = len(rs)
    if n < 2:
        return 0.0
    downside = [(r - target) ** 2 for r in rs if r < target]
    if not downside:
        return 0.0
    return math.sqrt(sum(downside) / (n - 1))


def sharpe_ratio(returns: Sequence[float], risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    """Annualised Sharpe ratio: mean(excess) / std(excess, ddof=1) * sqrt(P)."""
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0
    excess = [r - risk_free / periods_per_year for r in rs]
    if stdev(excess) == 0:
        return 0.0
    return _finite_or_zero(
        float(np.mean(excess)) / float(np.std(excess, ddof=1)) * math.sqrt(periods_per_year)
    )


def sharpe_standard_error(
    sharpe: float, n_observations: int, periods_per_year: int = 252
) -> float:
    """Lo (2002) asymptotic standard error of an annualised Sharpe ratio."""
    if n_observations < 2:
        return 0.0
    per_period = sharpe / math.sqrt(periods_per_year)
    return _finite_or_zero(
        math.sqrt((1.0 + 0.5 * per_period**2) / n_observations) * math.sqrt(periods_per_year)
    )


def sortino_ratio(returns: Sequence[float], risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    """Annualised Sortino ratio with population downside denominator."""
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0
    excess = [r - risk_free / periods_per_year for r in rs]
    if downside_deviation(excess, target=0.0) == 0:
        return 0.0
    downside = math.sqrt(sum(r * r for r in excess if r < 0) / len(excess))
    return _finite_or_zero(
        float(np.mean(excess)) / downside * math.sqrt(periods_per_year)
    )


def max_drawdown(returns: Sequence[float]) -> dict[str, float]:
    rs = _to_list(returns)
    if not rs:
        return {"max_drawdown": 0.0, "max_drawdown_duration": 0.0}
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    dur = 0
    max_dur = 0
    for r in rs:
        equity *= 1 + r
        if equity > peak:
            peak = equity
            dur = 0
        else:
            dur += 1
            dd = (equity / peak) - 1
            if dd < max_dd:
                max_dd = dd
        if dur > max_dur:
            max_dur = dur
    return {"max_drawdown": float(max_dd), "max_drawdown_duration": float(max_dur)}


def calmar_ratio(returns: Sequence[float], periods_per_year: int = 252) -> float:
    """CAGR / |max drawdown|."""
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0
    dd = abs(max_drawdown(rs)["max_drawdown"])
    if dd == 0:
        return 0.0
    years = len(rs) / periods_per_year
    growth = 1.0
    for r in rs:
        growth *= 1.0 + r
    cagr = abs(growth) ** (1.0 / years) - 1.0
    return _finite_or_zero(cagr / dd)


def beta(portfolio_returns: Sequence[float], benchmark_returns: Sequence[float]) -> float:
    pr = _to_list(portfolio_returns)
    br = _to_list(benchmark_returns)
    n = min(len(pr), len(br))
    if n < 2:
        return 0.0
    pr = pr[-n:]
    br = br[-n:]
    mp = mean(pr)
    mb = mean(br)
    cov = sum((p - mp) * (b - mb) for p, b in zip(pr, br)) / (n - 1)
    var_b = sum((b - mb) ** 2 for b in br) / (n - 1)
    if var_b == 0:
        return 0.0
    return cov / var_b


def alpha(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Annualised Jensen's alpha."""
    pr = _to_list(portfolio_returns)
    br = _to_list(benchmark_returns)
    n = min(len(pr), len(br))
    if n < 2:
        return 0.0
    pr = pr[-n:]
    br = br[-n:]
    b = beta(pr, br)
    ann_p = annualised_return(pr, periods_per_year)
    ann_b = annualised_return(br, periods_per_year)
    return ann_p - (risk_free + b * (ann_b - risk_free))


def r_squared(portfolio_returns: Sequence[float], benchmark_returns: Sequence[float]) -> float:
    pr = _to_list(portfolio_returns)
    br = _to_list(benchmark_returns)
    n = min(len(pr), len(br))
    if n < 2:
        return 0.0
    pr = pr[-n:]
    br = br[-n:]
    mp = mean(pr)
    mb = mean(br)
    cov = sum((p - mp) * (b - mb) for p, b in zip(pr, br)) / (n - 1)
    sd_p = stdev(pr)
    sd_b = stdev(br)
    if sd_p == 0 or sd_b == 0:
        return 0.0
    corr = cov / (sd_p * sd_b)
    return corr ** 2


def treynor_ratio(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Annualised Treynor ratio."""
    pr = _to_list(portfolio_returns)
    br = _to_list(benchmark_returns)
    n = min(len(pr), len(br))
    if n < 2:
        return 0.0
    pr = pr[-n:]
    br = br[-n:]
    b = beta(pr, br)
    if b == 0:
        return 0.0
    annret = annualised_return(pr, periods_per_year)
    return (annret - risk_free) / b


def skewness(returns: Sequence[float]) -> float:
    """Sample skewness (bias-corrected G1)."""
    rs = _to_list(returns)
    n = len(rs)
    if n < 3 or stdev(rs) == 0:
        return 0.0
    return _finite_or_zero(float(_scipy_skew(rs, bias=False)))


def kurtosis(returns: Sequence[float]) -> float:
    """Excess kurtosis (normal = 0), bias-corrected G2."""
    rs = _to_list(returns)
    n = len(rs)
    if n < 4 or stdev(rs) == 0:
        return 0.0
    return _finite_or_zero(float(_scipy_kurtosis(rs, fisher=True, bias=False)))
