"""Expanded portfolio-risk metrics: Sortino, Calmar, CVaR, beta, drawdowns, etc.

Phase 2 (``docs/adr/0001-unified-portfolio-engine.md``) delegated
Sharpe/Sortino/Calmar/CVaR/volatility/skew/kurtosis to QuantStats rather than
reimplementing them. That delegation has since been fully unwound and the
dependency dropped. The reasons, in order of weight:

* **Estimator conventions we could not pin down.** VaR/CVaR came off first
  (see :func:`historical_var`) once QuantStats' "historical" VaR turned out to
  be parametric and its CVaR a hybrid that degenerates on short samples.
  Sharpe/Sortino followed, because ``rf=`` is gated on ``if rf > 0`` upstream,
  silently discarding the negative risk-free rates this app accepts as a query
  parameter. Both fixes left the call sites doing the real work anyway.
* **The remaining six calls were one-liners.** ``volatility`` is
  ``std(ddof=1) * sqrt(periods)``; ``skew``/``kurtosis`` forwarded straight to
  pandas. Carrying a 50-metric dependency for six formulas bought nothing.
* **It dragged a graphics stack into the API image.** QuantStats requires
  matplotlib, seaborn and tabulate. Nothing in ``app/`` imports any of them.

``tests/test_quant_metrics_goldens.py`` pins every affected function against
values generated from QuantStats 0.0.81 before the swap, so no published
number moved. Track-record significance (PSR/DSR/MinTRL/``expected_max_sharpe``)
was always custom and is unchanged, further down this file.
"""
from __future__ import annotations

import math
from itertools import combinations
from statistics import mean, stdev
from typing import Sequence

import numpy as np
import pandas as pd
from scipy.stats import kurtosis as _scipy_kurtosis
from scipy.stats import norm as _norm
from scipy.stats import skew as _scipy_skew
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.foundation.models.entities import TrialLedgerEntry

# Euler-Mascheroni constant, used by the False Strategy Theorem.
_EULER_MASCHERONI = 0.5772156649015328606

# Shared periods-per-year policy: trading-day cadence (market data, most
# metrics) vs. calendar-day cadence (e.g. paper-portfolio snapshots, which
# are taken daily including weekends). Callers should reference these
# instead of hardcoding 252/365 independently.
TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365


def _finite_or_zero(value: float) -> float:
    """Degenerate inputs (e.g. zero variance) produce NaN/Inf; this module's
    contract is to return 0.0 instead."""
    v = float(value)
    return v if math.isfinite(v) else 0.0


def _to_list(values: Sequence[float]) -> list[float]:
    """
    Convert a sequence to a list of floats, excluding None values.
    
    Parameters:
        values: A sequence of values, potentially containing None.
    
    Returns:
        list[float]: A list of non-None values converted to floats.
    """
    return [float(v) for v in values if v is not None]


def annualised_return(returns: Sequence[float], periods_per_year: int = 252) -> float:
    """Geometric compounding: prod(1+r)^(periods_per_year/N) - 1."""
    rs = _to_list(returns)
    n = len(rs)
    if n == 0:
        return 0.0
    # Guard against any (1+r) <= 0 which would make log undefined.
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
    """Sample semi-deviation: sqrt( sum((r-target)^2 for r<target) / (N-1) ).

    Uses N-1 denominator to match sample-std convention used throughout.
    """
    rs = _to_list(returns)
    n = len(rs)
    if n < 2:
        return 0.0
    downside = [(r - target) ** 2 for r in rs if r < target]
    if not downside:
        return 0.0
    return math.sqrt(sum(downside) / (n - 1))


def sharpe_ratio(returns: Sequence[float], risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    """Annualised Sharpe ratio: ``mean(excess) / std(excess, ddof=1) * sqrt(P)``.

    The risk-free rate is subtracted per period, not compounded. QuantStats
    (which used to back this) compounded it *and* gated ``rf=`` on ``if rf > 0``,
    silently discarding negative rates — ``risk_free`` is a user-supplied query
    parameter on ``GET /api/quant/portfolio/risk`` and euro rates were negative
    for most of 2015-2022.

    Note this is a point estimate with a very wide confidence interval on short
    samples: Lo (2002) gives ``SE(SR) = sqrt((1 + SR^2/2) / T)``, exposed as
    :func:`sharpe_standard_error`. Callers reporting a Sharpe to a human should
    report that alongside it.
    """
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
    """Lo (2002) asymptotic standard error of an *annualised* Sharpe ratio.

    ``SE(SR_p) = sqrt((1 + SR_p^2 / 2) / T)`` for the per-period Sharpe under
    IID returns; scaled by ``sqrt(periods_per_year)`` to match the annualised
    figure :func:`sharpe_ratio` returns.

    This exists because the point estimate alone is misleading at the sample
    sizes this app actually has. For an annualised Sharpe of 1.0 on daily data
    the 95% interval is roughly ±16.8 at T=5, ±4.8 at T=60 and still ±2.0 after
    a *full year* — Bailey & López de Prado's Minimum Track Record Length puts
    the observations needed to establish SR=1.0 > 0 at 95% confidence around
    989 daily returns (~2.7 years). Anything that renders a Sharpe to a human
    should render this beside it, or suppress the figure when the interval
    spans zero.

    Returns 0.0 for ``n_observations < 2``, matching this module's contract of
    never propagating NaN.
    """
    if n_observations < 2:
        return 0.0
    per_period = sharpe / math.sqrt(periods_per_year)
    return _finite_or_zero(
        math.sqrt((1.0 + 0.5 * per_period**2) / n_observations) * math.sqrt(periods_per_year)
    )


def sortino_ratio(returns: Sequence[float], risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    """Annualised Sortino ratio.

    The downside term uses a population (N) denominator over the full series
    length, not this module's sample-convention :func:`downside_deviation` —
    that divergence predates the QuantStats removal and is retained so no
    published Sortino moved. The no-downside short-circuit returns this
    module's 0.0 contract rather than NaN.

    The risk-free rate is handled as in :func:`sharpe_ratio` — see its
    docstring.
    """
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0
    excess = [r - risk_free / periods_per_year for r in rs]
    if downside_deviation(excess, target=0.0) == 0:
        return 0.0
    # Population (N) denominator on the downside term, where N is the length of
    # the *full* series, not just its negative part — the Red Rock Capital
    # convention QuantStats used, retained so no published Sortino moved.
    # Deliberately not this module's sample-convention `downside_deviation`,
    # which is used only for the zero-downside short-circuit above.
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
        # A period at-or-above the running peak is NOT underwater (drawdown
        # depth 0), so the underwater streak resets. Using ``>=`` (not strict
        # ``>``) is what makes a flat equity curve report duration 0 instead of
        # len-1, and matches the standard peak-to-peak / backtrader definition.
        if equity >= peak:
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
    """CAGR / |max drawdown|.

    CAGR compounds over ``len(returns) / periods_per_year`` years rather than
    over the calendar span of the series, so it is consistent with how every
    other ratio here annualises.
    """
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0
    if abs(max_drawdown(rs)["max_drawdown"]) == 0:
        return 0.0
    years = len(rs) / periods_per_year
    growth = 1.0
    for r in rs:
        growth *= 1.0 + r
    cagr = abs(growth) ** (1.0 / years) - 1.0
    return _finite_or_zero(cagr / abs(max_drawdown(rs)["max_drawdown"]))


def _var_threshold_index(n: int, confidence: float) -> int:
    """Index into an ascending-sorted return series of the VaR observation."""
    idx = int(math.floor((1.0 - confidence) * n))
    return min(max(idx, 0), n - 1)


def historical_var(returns: Sequence[float], confidence: float = 0.95) -> float:
    """Empirical historical VaR — the ``1 - confidence`` order statistic.

    Genuinely historical, unlike QuantStats' ``value_at_risk``, which despite
    the name is a *parametric* (Gaussian variance-covariance) estimate. Phase 2
    delegated this function to QuantStats to guarantee ``cvar >= var`` by
    sharing a tail threshold; that guarantee did not survive contact with
    QuantStats' internals (its ``cvar`` re-prepares an already-prepared series,
    and the price-detection heuristic that runs there is not idempotent), and
    the swap silently re-calibrated every consumer that pairs this against a
    fixed ceiling — most importantly ``advisor/risk_gate.py``'s documented
    3% daily ``var_95_daily`` limit, whose breach rate rose sharply on
    fat-tailed books because a Gaussian fit inflates sigma from the very tails
    it is meant to summarise. :func:`historical_cvar` now shares *this*
    threshold, so the invariant holds by construction and for real.

    Returned as a positive loss magnitude, floored at 0.
    """
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0
    ordered = sorted(rs)
    return max(0.0, -ordered[_var_threshold_index(len(ordered), confidence)])


def historical_cvar(returns: Sequence[float], confidence: float = 0.95) -> float:
    """Historical CVaR (Expected Shortfall): mean of the observations at or
    below the empirical VaR threshold from :func:`historical_var`.

    Phase 2 delegated this to QuantStats, whose CVaR is a *hybrid* estimator —
    a parametric (Gaussian) tail threshold, then a historical average below it.
    On a small sample that threshold routinely excludes every observation, and
    QuantStats then returns the VaR estimate itself: measured, that degenerate
    case fires in ~93% of samples at n=5 (``advisor/scorecard.py``'s guard) and
    ~25% at n=20 (the discover pipeline's guard). A "CVaR" that is really a
    parametric VaR is not an expected shortfall at all, and it was being
    persisted to ``metrics_snapshots`` and rendered next to a VaR tile as if
    the two were independent measurements.

    Sharing the empirical threshold makes ``cvar >= var`` true by construction
    — the mean of a set of observations at or below the threshold cannot be
    above it — rather than by appeal to a QuantStats implementation detail.

    Returned as a positive loss magnitude, floored at 0.
    """
    rs = _to_list(returns)
    if not rs:
        return 0.0
    if len(rs) == 1:
        return max(0.0, -rs[0])
    ordered = sorted(rs)
    tail = ordered[: _var_threshold_index(len(ordered), confidence) + 1]
    return max(0.0, -(sum(tail) / len(tail)))


def parametric_cvar(returns: Sequence[float], confidence: float = 0.95) -> float:
    """Parametric (Gaussian) CVaR using exact normal quantile via scipy."""
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0
    mu = mean(rs)
    sigma = stdev(rs)
    z_alpha = _norm.ppf(confidence)
    # phi(z) = standard normal PDF at z_alpha
    phi_z = math.exp(-0.5 * z_alpha**2) / math.sqrt(2 * math.pi)
    cvar = -mu + sigma * phi_z / (1 - confidence)
    # cvar is already expressed as a loss (positive = loss). Floor at 0 rather
    # than abs() so a strongly positive-mean series can't flip to a fake loss.
    return max(0.0, cvar)


def skewness(returns: Sequence[float]) -> float:
    """Sample skewness (bias-corrected G1, matching pandas' ``.skew()``).

    Not the hand-rolled formula this module carried before Phase 2, which
    divided a population central moment by a sample std.
    """
    rs = _to_list(returns)
    n = len(rs)
    if n < 3 or stdev(rs) == 0:
        return 0.0
    return _finite_or_zero(float(_scipy_skew(rs, bias=False)))


def kurtosis(returns: Sequence[float]) -> float:
    """Excess kurtosis (normal = 0), bias-corrected G2, matching pandas'
    ``.kurtosis()``. See :func:`skewness`."""
    rs = _to_list(returns)
    n = len(rs)
    if n < 4 or stdev(rs) == 0:
        return 0.0
    return _finite_or_zero(float(_scipy_kurtosis(rs, fisher=True, bias=False)))


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
    """Annualised Jensen's alpha: the intercept of the excess-return regression.

    ``r_p - rf = a + b (r_b - rf) + e`` per period, reported as ``a * P``
    (arithmetic annualisation, Jensen 1968). The series must already be
    aligned on the same dates (see :func:`align_by_date`); here they are
    only trimmed to a common trailing length.

    The previous form, ``CAGR_p - (rf + beta (CAGR_b - rf))``, mixed geometric
    annualised returns with an arithmetic beta and drifted from the
    regression intercept by roughly ``(sigma_p^2 - beta sigma_b^2) / 2`` a
    year, which is large for a volatile or short sample.
    """
    pr = _to_list(portfolio_returns)
    br = _to_list(benchmark_returns)
    n = min(len(pr), len(br))
    if n < 2:
        return 0.0
    pr = pr[-n:]
    br = br[-n:]
    rf = risk_free / periods_per_year
    b = beta(pr, br)
    a = (mean(pr) - rf) - b * (mean(br) - rf)
    return _finite_or_zero(a * periods_per_year)


def align_by_date(
    left: dict[str, float], right: dict[str, float]
) -> tuple[list[str], list[float], list[float]]:
    """Inner-join two ``{date: return}`` maps: (dates, left values, right values).

    Every metric that pairs two return streams (beta, alpha, R^2, the
    regression scatter) must pair them by calendar date. Pairing by trailing
    position silently compares different days whenever either series has a
    gap, a holiday or a different start, which drives correlation to zero.
    """
    keys = sorted(set(left) & set(right))
    return keys, [float(left[k]) for k in keys], [float(right[k]) for k in keys]


def benchmark_regression(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
    hac_lags: int | None = None,
) -> dict[str, float | int | None]:
    """OLS of excess portfolio returns on excess benchmark returns, date-aligned inputs.

    Returns ``alpha`` (annualised intercept), ``alpha_t_stat`` (Newey-West
    HAC standard error, Bartlett kernel, ``floor(4 (n/100)^(2/9))`` lags by
    default), ``beta`` with ``beta_t_stat``, ``r_squared``, ``beta_dimson``
    (the sum of the lead, contemporaneous and lagged slopes, Dimson 1979,
    which corrects beta for markets that close at different times),
    ``tracking_error`` and ``information_ratio`` (both annualised) and ``n``.
    Values are ``None`` when the sample is too short to estimate them.
    """
    pr = np.asarray(_to_list(portfolio_returns), dtype=float)
    br = np.asarray(_to_list(benchmark_returns), dtype=float)
    n = min(len(pr), len(br))
    out: dict[str, float | int | None] = {
        "n": n, "alpha": None, "alpha_t_stat": None, "beta": None, "beta_t_stat": None,
        "r_squared": None, "beta_dimson": None, "tracking_error": None, "information_ratio": None,
    }
    if n < 3:
        return out
    pr, br = pr[-n:], br[-n:]
    rf = risk_free / periods_per_year
    y, x = pr - rf, br - rf
    if float(np.var(x)) == 0.0:
        return out
    X = np.column_stack([np.ones(n), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    ss_tot = float(((y - y.mean()) ** 2).sum())
    out["alpha"] = float(coef[0]) * periods_per_year
    out["beta"] = float(coef[1])
    out["r_squared"] = 1.0 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else None
    lags = hac_lags if hac_lags is not None else int(4 * (n / 100.0) ** (2.0 / 9.0))
    # Newey-West sandwich: (X'X)^-1 S (X'X)^-1 with Bartlett weights.
    xu = X * resid[:, None]
    S = xu.T @ xu
    for lag in range(1, min(lags, n - 1) + 1):
        w = 1.0 - lag / (lags + 1.0)
        g = xu[lag:].T @ xu[:-lag]
        S += w * (g + g.T)
    bread = np.linalg.inv(X.T @ X)
    cov = bread @ S @ bread * n / max(n - 2, 1)
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    out["alpha_t_stat"] = float(coef[0] / se[0]) if se[0] > 0 else None
    out["beta_t_stat"] = float(coef[1] / se[1]) if se[1] > 0 else None
    active = pr - br
    te = float(np.std(active, ddof=1)) * math.sqrt(periods_per_year)
    out["tracking_error"] = te
    out["information_ratio"] = float(np.mean(active)) * periods_per_year / te if te > 0 else None
    if n >= 6:
        # Dimson: y_t on x_{t-1}, x_t, x_{t+1}; beta = sum of the three slopes.
        Xd = np.column_stack([np.ones(n - 2), x[:-2], x[1:-1], x[2:]])
        cd, *_ = np.linalg.lstsq(Xd, y[1:-1], rcond=None)
        out["beta_dimson"] = float(cd[1] + cd[2] + cd[3])
    return {k: (_finite_or_zero(v) if isinstance(v, float) else v) for k, v in out.items()}


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
    """Annualised Treynor ratio.

    Both return streams are aligned to a common trailing window (as ``beta``
    and ``alpha`` do) before annualisation — see ``alpha``'s docstring for
    why annualising a series against a mismatched window produces absurd
    results.
    """
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


def rolling_sharpe(returns: Sequence[float], window: int = 60, risk_free: float = 0.0) -> list[float]:
    """Rolling annualised Sharpe ratio vectorised with pandas rolling.

    Uses sample std (ddof=1), matching sharpe_ratio().  Returns one value per
    complete window, identical to the previous per-window loop behaviour.
    """
    rs = _to_list(returns)
    if len(rs) < window:
        return []
    daily_rf = risk_free / 252  # periods_per_year not passed here; use default 252
    excess = pd.Series([r - daily_rf for r in rs])
    roll_mean = excess.rolling(window).mean()
    roll_std = excess.rolling(window).std(ddof=1)
    # Avoid division by zero; where std == 0, Sharpe is 0.
    sharpe = (roll_mean / roll_std.replace(0.0, float("nan"))) * math.sqrt(252)
    sharpe = pd.Series(np.nan_to_num(sharpe, nan=0.0, posinf=0.0, neginf=0.0))
    # Drop the first (window-1) NaN entries produced by rolling.
    return sharpe.iloc[window - 1 :].tolist()


def full_risk_report(
    returns: Sequence[float],
    benchmark_returns: Sequence[float] | None = None,
    risk_free: float = 0.0,
    confidence: float = 0.95,
    periods_per_year: int = 252,
) -> dict:
    """Full risk/return bundle over a per-period return series.

    Every annualised metric threads ``periods_per_year`` through to this
    module's canonical primitives; callers choose the cadence constant that
    matches their data (see the convention table in
    ``docs/adr/0003-quant-metrics-conventions.md``, decision 4).

    Reconciliation note (same ADR decision, audit §10 matrix #2): this bundle
    and ``performance_ledger.ex_post_risk.calculate_ex_post_risk`` overlap
    heavily but are KEPT DISTINCT on purpose — parameterize, don't flatten.
    They disagree numerically in skewness/kurtosis estimators (bias-corrected
    G1/G2 here vs raw scipy moments there), downside definition
    (unannualised sample semi-deviation here vs annualised negative-only
    volatility there), and output shape. See the extended note on
    ``calculate_ex_post_risk``.
    """
    rs = _to_list(returns)
    payload: dict[str, float | int | dict | None] = {
        "samples": len(rs),
        "annualised_return": annualised_return(rs, periods_per_year),
        "annualised_volatility": annualised_volatility(rs, periods_per_year),
        "downside_deviation": downside_deviation(rs, target=risk_free / periods_per_year),
        "sharpe": sharpe_ratio(rs, risk_free, periods_per_year),
        "sortino": sortino_ratio(rs, risk_free, periods_per_year),
        "calmar": calmar_ratio(rs, periods_per_year),
        "skewness": skewness(rs),
        "kurtosis_excess": kurtosis(rs),
        "historical_cvar": historical_cvar(rs, confidence),
        "parametric_cvar": parametric_cvar(rs, confidence),
        "drawdown": max_drawdown(rs),
    }
    if benchmark_returns is not None:
        payload["beta"] = beta(rs, benchmark_returns)
        payload["alpha"] = alpha(rs, benchmark_returns, risk_free, periods_per_year)
        payload["r_squared"] = r_squared(rs, benchmark_returns)
        payload["treynor"] = treynor_ratio(rs, benchmark_returns, risk_free, periods_per_year)
        reg = benchmark_regression(rs, benchmark_returns, risk_free, periods_per_year)
        payload["benchmark_samples"] = reg["n"]
        for key in ("alpha_t_stat", "beta_dimson", "tracking_error", "information_ratio"):
            payload[key] = reg[key]
    return payload


def risk_series(
    returns: Sequence[float],
    dates: Sequence[str] | None = None,
    benchmark_returns: Sequence[float] | dict[str, float] | None = None,
    *,
    base: float = 100.0,
    beta_window: int = 63,
) -> dict:
    """Build chart-ready series from a portfolio return stream.

    These complement the scalar metrics in :func:`full_risk_report` and feed the
    QuantLab Overview/Risk charts. Returned keys:

    - ``equity_curve``      ``[{date, value}]`` cumulative growth index (base 100)
    - ``rolling_drawdown``  ``[{date, value}]`` ``equity / cummax - 1`` (``<= 0``)
    - ``returns``           ``[float]``         the raw daily returns (for histograms)
    - ``regression_points`` ``[[bench, port]]`` aligned scatter vs the benchmark
    - ``rolling_beta``      ``[{date, value}]`` trailing-window beta vs the benchmark

    A ``{date: return}`` benchmark is joined on ``dates`` (see
    :func:`align_by_date`); a plain list is taken as already aligned and
    paired on the trailing ``min(len)`` observations, as :func:`beta` does.
    """
    rs = _to_list(returns)
    n = len(rs)
    ds = [str(d) for d in dates] if dates is not None else [str(i) for i in range(n)]
    if len(ds) != n:
        ds = ds[-n:] if len(ds) >= n else ds + [""] * (n - len(ds))

    equity_curve: list[dict] = []
    rolling_drawdown: list[dict] = []
    level = base
    peak = base
    for d, r in zip(ds, rs):
        level *= 1.0 + r
        peak = max(peak, level)
        equity_curve.append({"date": d, "value": round(level, 6)})
        dd = (level / peak - 1.0) if peak > 0 else 0.0
        rolling_drawdown.append({"date": d, "value": round(dd, 6)})

    out: dict = {
        "equity_curve": equity_curve,
        "rolling_drawdown": rolling_drawdown,
        "returns": [round(r, 8) for r in rs],
        "regression_points": [],
        "rolling_beta": [],
    }

    if benchmark_returns is not None:
        if isinstance(benchmark_returns, dict):
            ds_al, pr_al, br_al = align_by_date(dict(zip(ds, rs)), benchmark_returns)
            m = len(ds_al)
        else:
            br = _to_list(benchmark_returns)
            m = min(len(rs), len(br))
            pr_al, br_al, ds_al = rs[-m:], br[-m:], ds[-m:]
        if m >= 2:
            out["regression_points"] = [[round(b, 8), round(p, 8)] for b, p in zip(br_al, pr_al)]
            window = max(5, min(beta_window, m))
            rb: list[dict] = []
            for i in range(window, m + 1):
                rb.append({"date": ds_al[i - 1], "value": round(beta(pr_al[i - window:i], br_al[i - window:i]), 6)})
            out["rolling_beta"] = rb
    return out


def kelly_fraction(
    returns: Sequence[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Optimal fraction of capital to bet (Kelly criterion).

    Computes f* = (mu - r) / sigma^2, where mu is the expected return,
    r is the risk-free rate, and sigma^2 is the variance.

    This is the simplified Kelly formula for a single asset. The result
    represents the fraction of wealth to allocate for maximum long-run
    growth rate. Negative values indicate a short position.

    Args:
        returns: Return series (daily).
        risk_free: Annual risk-free rate (default 0).
        periods_per_year: Trading periods per year (252 for daily).

    Returns:
        Kelly fraction (e.g., 0.5 means bet 50% of capital).
    """
    rs = _to_list(returns)
    n = len(rs)
    if n < 2:
        return 0.0

    mu = mean(rs)
    sigma_sq = sum((r - mu) ** 2 for r in rs) / (n - 1)
    if sigma_sq == 0:
        return 0.0

    excess = mu - risk_free / periods_per_year
    return excess / sigma_sq


def volatility_drag(
    returns: Sequence[float],
    periods_per_year: int = 252,
) -> float:
    """Volatility drag: the reduction in compound returns due to volatility.

    Approximated as: drag = sigma^2 / 2 (annualized).

    This captures the mathematical reality that for a given arithmetic mean,
    higher volatility reduces the geometric (compound) return. The relationship
    is: geometric_return ≈ arithmetic_return - volatility_drag.

    Args:
        returns: Return series (daily).
        periods_per_year: Trading periods per year (252 for daily).

    Returns:
        Annualised volatility drag (always non-negative).
    """
    rs = _to_list(returns)
    if len(rs) < 2:
        return 0.0

    mu = mean(rs)
    sigma_sq = sum((r - mu) ** 2 for r in rs) / (len(rs) - 1)
    # Annualise variance then divide by 2
    return (sigma_sq * periods_per_year) / 2.0


# ---------------------------------------------------------------------------
# Track-record significance: Probabilistic / Deflated Sharpe Ratio & MinTRL.
#
# These implement Bailey & López de Prado's framework for deciding whether an
# observed Sharpe ratio reflects genuine skill rather than luck or the
# selection bias that arises from trying many strategies. All formulas operate
# on the *non-annualised* (per-period) Sharpe ratio, which is what the original
# papers use.
#
# References:
#   - Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier"
#     (Probabilistic Sharpe Ratio, Minimum Track Record Length).
#   - Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting
#     for Selection Bias, Backtest Overfitting and Non-Normality".
# ---------------------------------------------------------------------------


def _per_period_sharpe_moments(
    returns: Sequence[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> tuple[float, float, float, int]:
    """
    Compute statistical moments needed for probabilistic Sharpe ratio analysis.
    
    Extracts the per-period Sharpe ratio, skewness, and kurtosis from the given
    returns. Kurtosis is the non-excess fourth moment (a normal distribution has
    kurtosis 3.0), following the convention in Bailey & López de Prado.
    
    Parameters:
        returns: Sequence of period returns.
        risk_free: Annualized risk-free rate (default 0.0).
        periods_per_year: Number of periods per year (default 252).
    
    Returns:
        Tuple of (sharpe, skewness, kurtosis, n) where sharpe is the per-period
        Sharpe ratio, skewness and kurtosis are standardized moments, and n is
        the sample size.
    """
    rs = _to_list(returns)
    n = len(rs)
    if n < 3:
        return 0.0, 0.0, 3.0, n
    rf_per_period = risk_free / periods_per_year
    excess = [r - rf_per_period for r in rs]
    sd = stdev(excess)
    if sd == 0:
        return 0.0, 0.0, 3.0, n
    sr = mean(excess) / sd
    skew = float(_scipy_skew(rs, bias=False))
    # fisher=False -> normal distribution has kurtosis 3 (non-excess).
    kurt = float(_scipy_kurtosis(rs, fisher=False, bias=False))
    if not math.isfinite(skew):
        skew = 0.0
    if not math.isfinite(kurt):
        kurt = 3.0
    return sr, skew, kurt, n


def probabilistic_sharpe_ratio(
    returns: Sequence[float],
    sr_benchmark: float = 0.0,
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Probability that the true Sharpe ratio exceeds ``sr_benchmark``.

    ``sr_benchmark`` is expressed as an *annualised* Sharpe threshold for
    caller convenience (e.g. 0.0 to test "is there any skill?"); it is
    converted to per-period internally. Returns a probability in [0, 1].
    """
    sr, skew, kurt, n = _per_period_sharpe_moments(returns, risk_free, periods_per_year)
    if n < 3:
        return 0.0
    sr_star = sr_benchmark / math.sqrt(periods_per_year)
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if denom <= 0:
        return 0.0
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(_norm.cdf(z))


def expected_max_sharpe(n_trials: int, variance_sharpe: float) -> float:
    """Expected maximum per-period Sharpe across ``n_trials`` skill-less trials.

    The "False Strategy Theorem": if you try N independent strategies that
    genuinely have zero skill, the best one still shows a positive Sharpe by
    chance. This is the threshold a real strategy must clear.

        E[max SR_n] ≈ sqrt(V) * [ (1-γ)·Φ⁻¹(1 - 1/N) + γ·Φ⁻¹(1 - 1/(N·e)) ]

    where ``V`` is the cross-sectional variance of the trials' Sharpe ratios and
    γ is the Euler-Mascheroni constant. ``variance_sharpe`` is per-period.
    """
    if n_trials <= 1 or variance_sharpe <= 0:
        return 0.0
    n = float(n_trials)
    gamma = _EULER_MASCHERONI
    term1 = (1.0 - gamma) * float(_norm.ppf(1.0 - 1.0 / n))
    term2 = gamma * float(_norm.ppf(1.0 - 1.0 / (n * math.e)))
    return math.sqrt(variance_sharpe) * (term1 + term2)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    n_trials: int,
    variance_sharpe: float | None = None,
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Deflated Sharpe Ratio: PSR against the expected-max-Sharpe threshold.

    Deflates the observed Sharpe for the number of independent ``n_trials``
    that were evaluated (e.g. competing mandates / strategy iterations), which
    is the dominant source of selection bias / backtest overfitting.

    ``variance_sharpe`` is the per-period cross-sectional variance of the
    trials' Sharpe ratios. When unknown, a conservative default of ``1/(n-1)``
    (the asymptotic variance of the Sharpe estimator under normality) is used.

    Returns a probability in [0, 1]; values ≥ 0.95 are typically required to
    reject the "no skill" hypothesis.
    """
    sr, _, _, n = _per_period_sharpe_moments(returns, risk_free, periods_per_year)
    if n < 3:
        return 0.0
    if variance_sharpe is None:
        variance_sharpe = 1.0 / (n - 1)
    sr0_per_period = expected_max_sharpe(n_trials, variance_sharpe)
    # PSR evaluated at the deflated (annualised) threshold.
    sr0_annual = sr0_per_period * math.sqrt(periods_per_year)
    return probabilistic_sharpe_ratio(
        returns,
        sr_benchmark=sr0_annual,
        risk_free=risk_free,
        periods_per_year=periods_per_year,
    )


def min_track_record_length(
    returns: Sequence[float],
    sr_benchmark: float = 0.0,
    confidence: float = 0.95,
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Minimum number of observations for the Sharpe to be significant.

    Answers: "how long must the track record be before we can assert, with
    ``confidence``, that the true Sharpe exceeds ``sr_benchmark``?" Accounts
    for skew and kurtosis. Returns ``inf`` if the observed Sharpe does not yet
    exceed the benchmark (significance is unreachable on current evidence).
    """
    sr, skew, kurt, n = _per_period_sharpe_moments(returns, risk_free, periods_per_year)
    if n < 3:
        return float("inf")
    sr_star = sr_benchmark / math.sqrt(periods_per_year)
    if sr <= sr_star:
        return float("inf")
    z = float(_norm.ppf(confidence))
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    # Extreme skew/kurtosis can drive the variance term non-finite or negative;
    # in that case significance is not reachable on current evidence.
    if not math.isfinite(denom) or denom <= 0.0:
        return float("inf")
    return 1.0 + denom * (z / (sr - sr_star)) ** 2


# OLS numerical floor shared by :func:`compute_mincer_zarnowitz` (variance
# guard and R² degenerate-case guard).
_EPS = 1e-6


def compute_mincer_zarnowitz(
    pairs: list[tuple[float, float]],
) -> tuple[float | None, float | None]:
    """Axis 3: OLS ``realised = a + b * predicted`` → (slope b, R²).

    A well-calibrated magnitude forecaster has slope ≈ 1 with high R².
    """
    if len(pairs) < 3:
        return None, None
    predicted = np.array([p for p, _ in pairs], dtype=float)
    realised = np.array([r for _, r in pairs], dtype=float)
    if float(np.var(predicted)) < _EPS:
        return None, None
    design = np.column_stack([np.ones(len(predicted)), predicted])
    coeffs, *_ = np.linalg.lstsq(design, realised, rcond=None)
    fitted = design @ coeffs
    ss_res = float(((realised - fitted) ** 2).sum())
    ss_tot = float(((realised - realised.mean()) ** 2).sum())
    r2 = 0.0 if ss_tot < _EPS else 1.0 - ss_res / ss_tot
    return float(coeffs[1]), float(r2)


#: Harvey-Liu-Zhu (RFS 2016) multiple-testing threshold for newly mined signals.
HLZ_T_THRESHOLD = 3.0


def newey_west_t_stat(series: Sequence[float] | np.ndarray, lags: int) -> float:
    """Bartlett-kernel Newey-West t-statistic of the sample mean.

    Robust to autocorrelation (overlapping forward-return windows make IC
    series serially dependent). Returns 0.0 when degenerate — which fails
    any significance gate.

    Promoted from ``alphacrafter/tuning.py`` (pure numpy, no decision-loop
    dependencies) so any consumer — not just the AlphaCrafter tuner — can
    reuse the same significance-test primitive rather than a third
    reimplementation.
    """
    x = np.asarray(series, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 3:
        return 0.0
    if float(np.ptp(x)) == 0.0:
        return 0.0
    sample_mean = float(x.mean())
    demeaned = x - sample_mean
    long_run = float((demeaned ** 2).mean())
    for lag in range(1, min(lags, n - 1) + 1):
        weight = 1.0 - lag / (lags + 1.0)
        cov = float((demeaned[lag:] * demeaned[:-lag]).mean())
        long_run += 2.0 * weight * cov
    se = np.sqrt(max(long_run, 0.0) / n)
    if not np.isfinite(se) or se < 1e-12:
        return 0.0
    return sample_mean / float(se)


def deflated_ic_sharpe(
    ic_series: Sequence[float] | np.ndarray,
    n_trials: int,
    variance_icir: float | None = None,
) -> float:
    """Probability that the observed ICIR clears the trial-deflated threshold.

    Bailey & López de Prado (JPM 2014) applied to an information-coefficient
    series: PSR-style z-score of the observed ICIR against the expected
    maximum ICIR under ``n_trials`` independent configurations. Monotone in
    ``n_trials`` — more trials raise the bar.

    Why not :func:`deflated_sharpe_ratio`? That one consumes a strategy
    RETURNS series (unbounded, annualised at ``periods_per_year``,
    Sharpe-semantics moments). Per-config evaluation yields an IC SERIES —
    bounded [-1, 1] cross-sectional correlation coefficients whose null
    distribution and moment scaling differ — so the returns-based estimator
    does not apply to it. The expected-maximum term itself IS reused from
    :func:`expected_max_sharpe` because the order-statistic formula is
    ratio-agnostic.

    Promoted from ``alphacrafter/tuning.py`` alongside :func:`newey_west_t_stat`.
    """
    x = np.asarray(ic_series, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 3 or n_trials < 1:
        return 0.0
    std = float(x.std(ddof=1))
    if std <= 0.0:
        return 0.0
    icir_hat = float(x.mean()) / std
    if variance_icir is None:
        variance_icir = 1.0 / (n - 1)
    threshold = expected_max_sharpe(n_trials, variance_icir)
    skew = float(_scipy_skew(x))
    kurt = float(_scipy_kurtosis(x, fisher=False))
    denom = 1.0 - skew * icir_hat + ((kurt - 1.0) / 4.0) * icir_hat * icir_hat
    if not np.isfinite(denom) or denom <= 0.0:
        return 0.0
    z = (icir_hat - threshold) * np.sqrt(n - 1) / np.sqrt(denom)
    return float(_norm.cdf(z))


#: Lower bound of the ledger-backed n_trials resolver. A single trial is
#: deflated against nothing (the DSR hurdle is then a plain PSR against 0).
#: The 500 floor ADR 0015 ruling #9 used had no source behind it and made
#: every gate read 500 while 18 trials had actually been run (2026-10-04
#: review); the gate now uses the real count and the Evidence page shows how
#: the hurdle moves with N instead.
DEFAULT_N_TRIALS_FLOOR = 1


def record_trial(
    db: Session,
    context: str,
    trial_key: str,
    metadata: dict | None = None,
) -> TrialLedgerEntry:
    """Idempotently append one row to the global trial ledger.

    ``(context, trial_key)`` must uniquely identify one independent
    hypothesis test; a duplicate call with the same key is a no-op (mirrors
    ``AcTrialLedger``'s ``uq_ac_trial_append`` idempotency pattern) so a
    retried job cannot inflate ``n_trials``. Every context that searches
    over configurations/hypotheses — AlphaCrafter's grid, an advisor
    challenger spawn, a discover candidate cohort, a graduation portfolio
    variant — calls this once per trial it evaluates.
    """
    existing = db.execute(
        select(TrialLedgerEntry).where(
            TrialLedgerEntry.context == context,
            TrialLedgerEntry.trial_key == trial_key,
        )
    ).scalars().first()
    if existing is not None:
        return existing
    entry = TrialLedgerEntry(context=context, trial_key=trial_key, metadata_json=metadata or {})
    db.add(entry)
    db.flush()
    return entry


def resolve_n_trials(db: Session, floor: int = DEFAULT_N_TRIALS_FLOOR) -> int:
    """Global experiment count for Deflated Sharpe deflation (ADR 0015).

    Counts every row ever written to the trial ledger, across every
    context: search breadth spent in one context still raises the bar
    everywhere else, so the gate cannot be gamed by moving the search into
    an uncounted corner. ``floor`` only keeps the count at least 1.
    """
    count = db.execute(select(func.count()).select_from(TrialLedgerEntry)).scalar_one()
    return max(int(count), floor)


def trial_family(context: str, trial_key: str, metadata: dict | None) -> str:
    """The search a trial belongs to: one tuning run, one pre-registered study.

    Configurations of one grid share data, period and signal, so their
    returns are highly correlated and they are not independent tests.
    """
    meta = metadata or {}
    family = meta.get("family") or meta.get("job_run_id") or meta.get("study") or trial_key
    return f"{context}:{family}"


def effective_n_trials(db: Session) -> int:
    """Lower bound on the number of independent trials: distinct search families.

    Lopez de Prado (2019) estimates N_eff by clustering the trials' return
    correlations; the ledger stores no return series, so each family (see
    :func:`trial_family`) counts once. The truth lies between this and
    :func:`resolve_n_trials`; the gate uses the conservative upper end.
    """
    rows = db.execute(
        select(TrialLedgerEntry.context, TrialLedgerEntry.trial_key, TrialLedgerEntry.metadata_json)
    ).all()
    return max(1, len({trial_family(c, k, m) for c, k, m in rows}))


def dsr_hurdle_curve(
    n_values: Sequence[int], n_observations: int, periods_per_year: int = 252
) -> list[dict[str, float]]:
    """Annualised Sharpe a strategy needs just to match luck, for each N.

    The expected maximum Sharpe of N skill-less trials (False Strategy
    Theorem, Bailey and Lopez de Prado 2014) with the null variance of a
    per-period Sharpe estimate, ``1 / n_observations``. A strategy below the
    curve at the ledger's N is indistinguishable from the best of N coin flips.
    """
    if n_observations < 2:
        return []
    var = 1.0 / n_observations
    out = []
    for n in sorted({int(v) for v in n_values if int(v) >= 1}):
        hurdle = expected_max_sharpe(n, var) * math.sqrt(periods_per_year) if n > 1 else 0.0
        out.append({"n_trials": n, "sharpe_hurdle": round(hurdle, 4)})
    return out


def _cscv_column_sharpe(block: np.ndarray) -> np.ndarray:
    """Per-column (per-strategy) Sharpe over one CSCV train/test block.

    Degenerate columns (zero variance — a strategy with a constant return
    in this block) are ranked worst rather than raising, since a flat
    sub-series carries no evidence either way.
    """
    mu = block.mean(axis=0)
    sd = block.std(axis=0, ddof=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        sr = np.where(sd > 0, mu / sd, -np.inf)
    return sr


def probability_of_backtest_overfitting(
    returns_matrix: np.ndarray | pd.DataFrame,
    n_partitions: int = 16,
) -> float:
    """Combinatorially Symmetric Cross-Validation (CSCV) estimate of PBO.

    Bailey, Borwein, López de Prado & Zhu (2016), "The Probability of
    Backtest Overfitting". ``returns_matrix`` is (T periods x N candidate
    strategies/configs); each column is one candidate's return series over
    the SAME periods as every other column (i.e. this is the search-space
    matrix a selection was made from, not a single strategy's history).

    Splits the T rows into ``n_partitions`` contiguous blocks and forms
    every combination that assigns half the blocks to a training set and
    the complementary half to a test set. For each split: pick the
    in-sample (training) best-Sharpe column, then find that SAME column's
    rank among all N columns' out-of-sample (test) Sharpes. If it ranks
    below the test-set median, the in-sample winner did not generalise —
    that split "votes" overfit. PBO is the fraction of splits that vote
    overfit.

    Returns 1.0 (maximally overfit — fails any ``<=`` gate) when there is
    not enough data/candidates to assess honestly, matching this codebase's
    fail-closed convention for gate inputs rather than silently reporting
    an optimistic 0.0.
    """
    m = np.asarray(returns_matrix, dtype=float)
    if m.ndim != 2:
        raise ValueError("returns_matrix must be 2-D (periods x candidate strategies)")
    t, n = m.shape
    if n < 2 or n_partitions < 2 or n_partitions % 2 != 0:
        return 1.0
    if t < n_partitions * 2:
        return 1.0

    edges = np.linspace(0, t, n_partitions + 1, dtype=int)
    blocks = [m[edges[i]:edges[i + 1]] for i in range(n_partitions)]
    half = n_partitions // 2

    logits: list[float] = []
    for combo in combinations(range(n_partitions), half):
        train_idx = set(combo)
        train = np.vstack([blocks[i] for i in sorted(train_idx)])
        test = np.vstack([blocks[i] for i in range(n_partitions) if i not in train_idx])
        train_sr = _cscv_column_sharpe(train)
        test_sr = _cscv_column_sharpe(test)
        best = int(np.argmax(train_sr))
        # Relative rank (1..N) of the in-sample winner's OOS Sharpe; omega in
        # (0, 1) is its percentile — the paper's "logit" statistic.
        rank = 1 + int(np.sum(test_sr < test_sr[best]))
        omega = rank / (n + 1.0)
        if omega <= 0.0 or omega >= 1.0:
            continue
        logits.append(math.log(omega / (1.0 - omega)))

    if not logits:
        return 1.0
    below_median = sum(1 for lam in logits if lam <= 0.0)
    return below_median / len(logits)


def compute_brier_and_log_loss(
    pairs: list[tuple[float, int]],
) -> tuple[float | None, float | None]:
    """Calibration primitives from (confidence, hit) pairs: Brier score + log-loss.

    Promoted from ``advisor/scorecard.py`` (pure math, no decision-loop
    dependencies) so any consumer — not just advisor — can score calibration
    without importing through the advisor facade.
    """
    if not pairs:
        return None, None
    briers = [(p - h) ** 2 for p, h in pairs]
    log_losses = []
    for p, h in pairs:
        clipped = min(1.0 - _EPS, max(_EPS, p))
        log_losses.append(-(h * math.log(clipped) + (1 - h) * math.log(1.0 - clipped)))
    return float(sum(briers) / len(briers)), float(sum(log_losses) / len(log_losses))


def compute_bucketed_rps(
    triples: list[tuple[float, float, float]],
) -> float | None:
    """Bucketed Ranked Probability Score from (p5, p95, realised) triples.

    3 ordered categories — below p5, between p5 and p95, above p95 — with
    predicted cumulative probabilities fixed at (0.05, 0.95) by construction
    (p5/p95 are exactly the 5th/95th percentiles of the distribution the
    forecast came from). Measures whether the *realised* outcome actually
    falls in the predicted band as often as it should — a genuine calibration
    check, unlike Brier on sign alone.

    Standard 3-category RPS:
        RPS = (1/2) * [(P(<=p5) - I(realised<=p5))^2
                      + (P(<=p95) - I(realised<=p95))^2]

    Returns the average RPS across *triples* (0 = perfectly calibrated,
    1 = worst possible for 3 categories), or ``None`` if empty.

    Promoted from ``advisor/scorecard.py`` (pure math, no decision-loop
    dependencies) — see that module for the fuller F11 background on why
    this replaced a sign-only Brier score for magnitude-distribution
    calibration.
    """
    if not triples:
        return None
    scores: list[float] = []
    for low, high, realised in triples:
        if high < low:
            low, high = high, low
        below = 1.0 if realised <= low else 0.0
        at_or_below_high = 1.0 if realised <= high else 0.0
        rps = ((0.05 - below) ** 2 + (0.95 - at_or_below_high) ** 2) / 2.0
        scores.append(rps)
    return float(sum(scores) / len(scores))


def holm_bonferroni(p_values: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values for testing many hypotheses at once.

    Sorts the raw p-values ascending and multiplies the i-th smallest by the
    number of hypotheses not yet rejected (``m - i``), carrying the running
    maximum so the adjusted values stay monotone, capped at 1. Controls the
    family-wise error rate without assuming independence — the replay's
    per-ingredient ("more voices") analysis and the live ingredient table
    both correct across ingredients this way (ADR 0019 §§4-5).
    """
    raw = [min(1.0, max(0.0, float(p))) for p in p_values]
    order = sorted(range(len(raw)), key=lambda i: raw[i])
    adjusted = [0.0] * len(raw)
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, min(1.0, (len(raw) - rank) * raw[idx]))
        adjusted[idx] = running
    return adjusted
