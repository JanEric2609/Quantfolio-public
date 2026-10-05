"""Tests for the cross-sectional vol-scaled forward-return label (ADR 0015
ruling #24) added to app.lab.quant_ml.labels."""
import numpy as np
import pandas as pd
import pytest

from app.lab.quant_ml.labels import vol_scaled_forward_return


def _make_price_panel(n_rows: int = 100, symbols: tuple[str, ...] = ("A", "B")) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.date_range("2026-01-01", periods=n_rows, freq="D")
    data = {}
    for sym in symbols:
        returns = rng.normal(0.0005, 0.01, n_rows)
        data[sym] = 100.0 * np.cumprod(1 + returns)
    return pd.DataFrame(data, index=dates)


def test_vol_scaled_forward_return_shape_matches_input():
    panel = _make_price_panel()

    labels = vol_scaled_forward_return(panel, horizon=20, vol_window=60)

    assert labels.shape == panel.shape
    assert list(labels.columns) == list(panel.columns)


def test_vol_scaled_forward_return_nan_at_head_and_tail():
    panel = _make_price_panel(n_rows=100)

    labels = vol_scaled_forward_return(panel, horizon=20, vol_window=60)

    # First vol_window rows: not enough trailing history for realized vol.
    assert labels.iloc[:59].isna().all().all()
    # Last horizon rows: no forward return available.
    assert labels.iloc[-20:].isna().all().all()


def test_vol_scaled_forward_return_matches_hand_computation():
    dates = pd.date_range("2026-01-01", periods=5, freq="D")
    prices = pd.DataFrame({"A": [100.0, 101.0, 102.0, 100.0, 110.0]}, index=dates)

    labels = vol_scaled_forward_return(prices, horizon=1, vol_window=2)

    # Row index 2 (price=102): forward return = 100/102 - 1; trailing vol over
    # rows [1,2] (pct_change of rows 0..2) with min_periods=2.
    daily_returns = prices["A"].pct_change()
    expected_vol = daily_returns.iloc[0:3].rolling(window=2, min_periods=2).std().iloc[-1]
    expected_fwd = 100.0 / 102.0 - 1.0
    assert labels["A"].iloc[2] == pytest.approx(expected_fwd / expected_vol, rel=1e-9)


def test_vol_scaled_forward_return_scales_by_symbols_own_vol_not_shared():
    dates = pd.date_range("2026-01-01", periods=80, freq="D")
    rng = np.random.default_rng(7)
    # Symbol A: low vol; Symbol B: high vol, same expected forward return magnitude.
    low_vol_returns = rng.normal(0.001, 0.001, 80)
    high_vol_returns = rng.normal(0.001, 0.05, 80)
    panel = pd.DataFrame({
        "LOW_VOL": 100.0 * np.cumprod(1 + low_vol_returns),
        "HIGH_VOL": 100.0 * np.cumprod(1 + high_vol_returns),
    }, index=dates)

    labels = vol_scaled_forward_return(panel, horizon=5, vol_window=30)

    # The two columns' label distributions should differ (not identical scaling).
    valid = labels.dropna()
    assert not valid["LOW_VOL"].equals(valid["HIGH_VOL"])
