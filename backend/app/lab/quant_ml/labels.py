"""Return labels for supervised ML: fixed-horizon, triple-barrier, meta-labelling,
and the vol-scaled cross-sectional forward return (ADR 0015 ruling #24).

Based on the triple-barrier labelling method from López de Prado (2018)
and Stefan Jansen's "Machine Learning for Trading" implementation.
"""
from __future__ import annotations

import pandas as pd


def fixed_horizon_labels(
    prices: list[float],
    horizon: int = 5,
    threshold: float = 0.0,
) -> list[int]:
    """Simple fixed-horizon labels: +1 (up), -1 (down) based on h-day return."""
    labels: list[int] = []
    for i in range(len(prices) - horizon):
        ret = prices[i + horizon] / prices[i] - 1
        if ret > threshold:
            labels.append(1)
        elif ret < -threshold:
            labels.append(-1)
        else:
            labels.append(0)
    return labels


def triple_barrier_labels(
    prices: list[float],
    upper_barrier: float = 0.02,
    lower_barrier: float = 0.02,
    time_horizon: int = 20,
) -> list[int]:
    """Triple-barrier labels as in López de Prado (2018).

    For each bar i look ahead up to time_horizon bars:
      - If cumulative return > upper_barrier  → label +1
      - If cumulative return < -lower_barrier → label -1
      - If neither hits within horizon        → label  0
    """
    labels: list[int] = []
    n = len(prices)
    for i in range(n - 1):
        label = 0
        p0 = prices[i]
        if p0 == 0:
            labels.append(0)
            continue
        for j in range(i + 1, min(i + time_horizon + 1, n)):
            ret = prices[j] / p0 - 1
            if ret >= upper_barrier:
                label = 1
                break
            if ret <= -lower_barrier:
                label = -1
                break
        labels.append(label)
    return labels


def meta_labels(
    primary_labels: list[int],
    primary_predictions: list[int],
) -> list[int]:
    """Meta-labelling: 1 if primary model was correct, 0 otherwise."""
    return [1 if pred == lbl else 0 for pred, lbl in zip(primary_predictions, primary_labels)]


def vol_scaled_forward_return(
    price_panel: pd.DataFrame,
    horizon: int = 20,
    vol_window: int = 60,
) -> pd.DataFrame:
    """Forward ``horizon``-day return per symbol, scaled by trailing realized vol.

    ADR 0015 ruling #24: "Prediction target: forward volatility-scaled
    return, cross-sectional." Unlike ``fixed_horizon_labels``/
    ``triple_barrier_labels`` above (single-ticker classification), this
    operates on a (date x symbol) panel and returns a continuous
    cross-sectional target: ``forward_return / trailing_realized_vol``, so a
    symbol's label is comparable against every other symbol's on the same
    date regardless of that symbol's own volatility regime.

    Args:
        price_panel: (date x symbol) close prices, chronologically sorted.
        horizon: Forward look-ahead window in rows (trading days).
        vol_window: Trailing window (rows) used to estimate realized
            volatility of daily returns, annualisation-free (this is a
            scaling factor, not a reported risk metric).

    Returns:
        (date x symbol) DataFrame of the scaled label. The last ``horizon``
        rows and first ``vol_window`` rows are NaN (no forward return / not
        enough trailing history yet) -- callers must drop or mask these
        before fitting, exactly like any lookahead-bounded label.
    """
    daily_returns = price_panel.pct_change(fill_method=None)
    trailing_vol = daily_returns.rolling(window=vol_window, min_periods=vol_window).std()
    forward_return = price_panel.shift(-horizon) / price_panel - 1.0
    safe_vol = trailing_vol.replace(0.0, pd.NA)
    return forward_return / safe_vol
