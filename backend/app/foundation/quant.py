import math
from statistics import mean, stdev
from typing import Any, cast

from scipy.stats import norm as _norm




def historical_var(returns: list[float], confidence: float = 0.95) -> float:
    """Empirical VaR at the given confidence level.

    Delegates to the canonical implementation
    :func:`quant_metrics.historical_var` (ADR 0003, decision 1). The public
    name and signature are kept so call sites don't churn.

    Convention note (documented change): the previous in-house copy used
    ``numpy.percentile(..., method="lower")``, whose floor index is
    ``floor((1-confidence) * (n-1))``; the canonical order statistic uses
    ``floor((1-confidence) * n)``. The two select neighbouring observations
    at some sample sizes, and series shorter than two observations now
    return 0.0. Golden values were re-captured against the canonical
    outputs in ``tests/test_var_consolidation.py``.
    """
    from app.foundation.quant_metrics import historical_var as _canonical_historical_var

    return _canonical_historical_var(returns, confidence)


def parametric_var(returns: list[float], confidence: float = 0.95) -> float:
    """Parametric (Gaussian) VaR.

    Uses scipy.stats.norm.ppf so any confidence level is handled correctly
    instead of the previous two-value lookup table.
    Returns a positive number (loss magnitude).
    """
    if len(returns) < 2:
        return 0.0
    z = _norm.ppf(confidence)
    # Loss magnitude at the low quantile (mean - z*sigma), floored at 0 so a
    # portfolio whose quantile is still a gain reports VaR 0, not a fake loss.
    return float(max(0.0, -(mean(returns) - z * stdev(returns))))


# The former pure-Python GBM fan-chart projection (with its private band
# helper) that lived in this module was retired: ADR 0003 decision 2 names
# quant_mc/engine.py the canonical Monte Carlo engine, and every caller now
# uses app.foundation.quant_mc.projection.gbm_fan_projection.


def price_matrix_to_returns(price_matrix: dict[str, dict[str, float]]) -> Any:
    """Convert a {symbol: {date: price}} matrix to aligned daily returns.

    Does NOT forward-fill missing prices. Forward-filling a missing price and
    then computing a percent change against it fabricates a spurious 0%
    return for the gap day(s) -- a well-documented non-synchronous/stale-
    trading bias (Fisher 1966, Scholes-Williams 1977, Dimson 1979) that
    silently deflates measured volatility and correlation whenever any
    symbol has a data gap (market holidays, a late-listed asset, a provider
    outage for one ticker, etc). pandas itself deprecated pct_change()'s
    implicit fill_method='pad' default for exactly this reason (GH #28461,
    #53520).

    Columns that are entirely empty are dropped first so that one wholly
    missing symbol doesn't wipe out every date once full-row overlap is
    required; the remaining columns must all have a real price on a given
    date for that date to survive.
    """
    import pandas as pd

    frame = pd.DataFrame(price_matrix).dropna(axis=1, how="all").dropna()
    # Normalize to a genuine DatetimeIndex: some callers build price_matrix
    # from plain date/ISO-string keys, and a str-indexed frame never matches
    # a Timestamp-indexed frame on an inner join (e.g. factor_exposures()),
    # silently producing an always-empty join. Callers that already pass
    # real Timestamp keys are unaffected (pd.to_datetime is idempotent).
    frame.index = pd.to_datetime(frame.index)
    # Dict keys arrive in insertion order, not date order.
    return frame.sort_index().pct_change(fill_method=None).dropna()


def portfolio_risk_from_price_matrix(price_matrix: Any, weights: dict[str, float] | None = None) -> dict:
    import pandas as pd

    # dropna(how="all") removes fully-empty rows; we do NOT ffill before pct_change
    # to avoid converting internal price gaps into spurious 0% return observations.
    # dropna() after pct_change removes rows where all assets had a gap on the same
    # day; rows with a partial gap are handled via the weighted sum (missing columns
    # contribute 0 weight to that day's portfolio return, which is conservative).
    frame = pd.DataFrame(price_matrix).dropna(how="all").dropna(axis=1, how="all")
    if frame.empty:
        return {
            "status": "unavailable",
            "message": "No price matrix available.",
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "rolling_volatility": [],
            "equity_curve": [],
        }
    returns = frame.sort_index().pct_change(fill_method=None).dropna(how="all")
    if returns.empty:
        return unavailable_portfolio()
    weights = weights or {column: 1 / len(frame.columns) for column in frame.columns}
    weight_series = pd.Series(weights).reindex(frame.columns).fillna(0)
    if weight_series.sum() == 0:
        weight_series = pd.Series(1 / len(frame.columns), index=frame.columns)
    else:
        weight_series = weight_series / weight_series.sum()
    portfolio_returns = returns.mul(weight_series, axis=1).sum(axis=1)
    equity = (1 + portfolio_returns).cumprod() * 10000
    drawdown = equity / equity.cummax() - 1
    volatility = portfolio_returns.rolling(21).std() * math.sqrt(252)
    annual_vol = portfolio_returns.std() * math.sqrt(252)
    annual_return = portfolio_returns.mean() * 252
    return {
        "status": "completed",
        "source": "current_weights",
        "weights": {key: float(value) for key, value in weight_series.items()},
        "sharpe": float(0 if annual_vol == 0 else annual_return / annual_vol),
        "max_drawdown": float(abs(drawdown.min())),
        "historical_var_95": historical_var(portfolio_returns.tolist(), 0.95),
        "rolling_volatility": [
            {"date": _index_date(idx), "value": float(value)}
            for idx, value in volatility.dropna().tail(260).items()
        ],
        "equity_curve": [
            {"date": _index_date(idx), "value": float(value)}
            for idx, value in equity.tail(520).items()
        ],
    }


def correlation_matrix_from_price_matrix(price_matrix: Any) -> dict:
    import pandas as pd

    # No ffill: internal gaps remain NaN so they are excluded from correlation
    # calculations rather than becoming spurious 0% returns.
    frame = pd.DataFrame(price_matrix).dropna(how="all").dropna(axis=1, how="all")
    returns = frame.sort_index().pct_change(fill_method=None).dropna(how="all")
    if returns.empty:
        return {"assets": list(frame.columns), "matrix": []}
    # Pairwise over the dates both assets have a return; at least 20 shared
    # days or the pair is unknown (null), never a fabricated 0 correlation.
    corr = returns.corr(min_periods=20)
    matrix = [[None if v != v else round(float(v), 4) for v in row] for row in corr.values.tolist()]
    return {"assets": list(corr.columns), "matrix": matrix}


def simple_efficient_frontier(expected_returns: dict[str, float], covariance: Any, points: int = 15) -> dict:
    """Genuine mean-variance efficient frontier via scipy.optimize.minimize.

    For each of `points` evenly-spaced target returns between the minimum-
    variance return and the maximum-return asset, solve:

        min   w' Σ w
        s.t.  w' μ = target_return
              sum(w) = 1
              w_i >= 0          (long-only)

    Returns the same dict shape as before: {"current", "frontier", "suggested_weights"}.
    Falls back to equal-weight if optimisation repeatedly fails (e.g. single asset).
    """
    import numpy as np
    import pandas as pd
    from scipy.optimize import minimize, Bounds

    assets = list(expected_returns)
    if not assets:
        return {"current": {}, "frontier": [], "suggested_weights": {}}

    n = len(assets)
    cov = pd.DataFrame(
        covariance, index=pd.Index(assets), columns=pd.Index(assets)
    ).fillna(0).to_numpy(dtype=float)
    mu = np.array([expected_returns[asset] for asset in assets], dtype=float)

    # Regularise covariance for numerical stability.
    cov = cov + np.eye(n) * 1e-8

    def _portfolio_vol(w: np.ndarray) -> float:
        return float(w @ cov @ w)

    def _min_var_weights() -> np.ndarray:
        """Unconstrained-return min-variance as starting point."""
        ones = np.ones(n)
        try:
            inv_cov_ones = np.linalg.solve(cov, ones)
            w = inv_cov_ones / float(ones @ inv_cov_ones)
            return np.clip(w, 0.0, None)
        except np.linalg.LinAlgError:
            return np.full(n, 1.0 / n)

    def _solve_for_target(target: float) -> np.ndarray | None:
        constraints = [
            {"type": "eq", "fun": lambda w: w.sum() - 1.0},
            {"type": "eq", "fun": lambda w, t=target: float(w @ mu) - t},
        ]
        bounds = Bounds(lb=0.0, ub=1.0)
        w0 = _min_var_weights()
        # Normalize w0, but handle the case where clipping produces all zeros
        total = w0.sum()
        if total > 0:
            w0 = w0 / total
        else:
            # Fallback to equal weights if all weights clipped to zero
            w0 = np.full(len(w0), 1.0 / len(w0))
        result = minimize(
            _portfolio_vol,
            w0,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"ftol": 1e-9, "maxiter": 500},
        )
        if result.success:
            w = np.clip(result.x, 0.0, None)
            s = w.sum()
            return w / s if s > 0 else None
        return None

    # Determine target-return range.
    mu_min_var = float(mu @ _min_var_weights())
    mu_max = float(mu.max())
    # Clamp so the range is always valid (mu_max may equal mu_min_var for 1-asset).
    if mu_max <= mu_min_var:
        mu_min_var = mu.min()
    targets = np.linspace(mu_min_var, mu_max, max(points, 2))

    frontier = []
    best = None
    for target in targets:
        w = _solve_for_target(float(target))
        if w is None:
            # Fallback: equal weight for this point.
            w = np.full(n, 1.0 / n)
        ret = float(w @ mu)
        vol = float(math.sqrt(max(float(w @ cov @ w), 0.0)))
        sharpe = 0.0 if vol == 0 else ret / vol
        point = {
            "return": ret,
            "volatility": vol,
            "sharpe": sharpe,
            "weights": {asset: float(wi) for asset, wi in zip(assets, w, strict=False)},
        }
        frontier.append(point)
        if best is None or point["sharpe"] > best["sharpe"]:
            best = point

    # Build current weights from the actual expected-returns dict
    total_mu = sum(max(m, 0.0) for m in mu)
    if total_mu > 0:
        current = {a: max(float(m), 0.0) / total_mu for a, m in zip(assets, mu, strict=False)}
    else:
        current = {a: 1.0 / n for a in assets}

    return {"current": current, "frontier": frontier, "suggested_weights": best["weights"] if best else {}}


def portfolio_optimizations_from_price_matrix(price_matrix: Any) -> dict:
    import numpy as np
    import pandas as pd

    # No ffill: preserve NaN gaps so they don't become spurious 0% returns.
    frame: pd.DataFrame = pd.DataFrame(price_matrix).dropna(how="all").dropna(axis=1, how="all")
    returns: pd.DataFrame = frame.sort_index().pct_change(fill_method=None).dropna(how="all")
    if returns.empty:
        return {"status": "unavailable", "methods": {}, "message": "No return matrix available."}
    assets = list(returns.columns)
    mu = cast(pd.Series, returns.mean(axis=0) * 252)
    cov = cast(pd.DataFrame, returns.cov() * 252)
    vol = cast(pd.Series, returns.std(axis=0) * math.sqrt(252))
    equal = cast(pd.Series, pd.Series(1 / len(assets), index=assets))
    inv_vol_raw = 1 / vol.replace(0, np.nan)
    inverse_vol = inv_vol_raw.fillna(0)
    inverse_vol = inverse_vol / inverse_vol.sum() if inverse_vol.sum() else equal
    frontier = simple_efficient_frontier(mu.to_dict(), cov)
    max_sharpe = pd.Series(frontier.get("suggested_weights") or equal.to_dict()).reindex(assets).fillna(0)
    # True global min-variance: w* = Σ^{-1}1 / (1ᵀΣ^{-1}1), clipped long-only.
    # np.linalg.solve is preferred over explicit inversion for numerical stability.
    cov_arr = cov.to_numpy(dtype=float) + np.eye(len(assets)) * 1e-8
    ones = np.ones(len(assets))
    try:
        inv_cov_ones = np.linalg.solve(cov_arr, ones)
        min_vol_raw_arr = inv_cov_ones / float(ones @ inv_cov_ones)
        min_vol_raw_arr = np.clip(min_vol_raw_arr, 0.0, None)
        min_vol_sum = min_vol_raw_arr.sum()
        if min_vol_sum > 0:
            min_vol_raw_arr = min_vol_raw_arr / min_vol_sum
        else:
            min_vol_raw_arr = ones / len(assets)
    except np.linalg.LinAlgError:
        min_vol_raw_arr = ones / len(assets)
    min_vol = pd.Series(min_vol_raw_arr, index=assets)
    methods = {
        "equal_weight": _portfolio_method_stats(equal, mu, cov),
        "inverse_volatility": _portfolio_method_stats(inverse_vol, mu, cov),
        "max_sharpe": _portfolio_method_stats(max_sharpe, mu, cov),
        "min_volatility": _portfolio_method_stats(min_vol, mu, cov),
    }
    return {"status": "completed", "methods": methods}


def factor_exposures(portfolio_returns: Any, factor_returns: Any) -> dict:
    import numpy as np
    import pandas as pd

    y = pd.Series(portfolio_returns, name="portfolio").dropna()
    x = pd.DataFrame(factor_returns).dropna()
    joined = pd.concat([y, x], axis=1, join="inner").dropna()
    if len(joined) < max(20, len(x.columns) + 5):
        return {"status": "unavailable", "message": "Not enough overlapping factor history.", "exposures": {}}
    y_values = joined["portfolio"].to_numpy(dtype=float)
    x_values = joined.drop(columns=["portfolio"]).to_numpy(dtype=float)
    x_design = np.column_stack([np.ones(len(x_values)), x_values])
    coefficients, *_ = np.linalg.lstsq(x_design, y_values, rcond=None)
    fitted = x_design.dot(coefficients)
    residual = y_values - fitted
    ss_res = float((residual**2).sum())
    ss_tot = float(((y_values - y_values.mean()) ** 2).sum())
    return {
        "status": "completed",
        "alpha_daily": float(coefficients[0]),
        "r_squared": 0.0 if ss_tot == 0 else float(1 - ss_res / ss_tot),
        "exposures": {
            column: float(value)
            for column, value in zip(joined.drop(columns=["portfolio"]).columns, coefficients[1:], strict=False)
        },
    }


def unavailable_portfolio() -> dict:
    return {
        "status": "unavailable",
        "source": "current_weights",
        "sharpe": 0.0,
        "max_drawdown": 0.0,
        "rolling_volatility": [],
        "equity_curve": [],
    }


def _portfolio_method_stats(weights: Any, expected_returns: Any, covariance: Any) -> dict:
    weight_series = weights / weights.sum() if weights.sum() else weights
    expected = float(weight_series.dot(expected_returns))
    volatility = float(math.sqrt(max(weight_series.dot(covariance).dot(weight_series), 0)))
    return {
        "expected_return": expected,
        "volatility": volatility,
        "sharpe": 0.0 if volatility == 0 else expected / volatility,
        "weights": {key: float(value) for key, value in weight_series.items()},
    }


def _index_date(value: Any) -> str:
    if hasattr(value, "date"):
        return value.date().isoformat()
    return str(value)
