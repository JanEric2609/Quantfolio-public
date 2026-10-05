"""Regression tests for build_regime_features hardening.

Covers the Aug 2026 production incidents in the daily regime job:
- pandas.errors.MergeError from tz-aware price ``ts`` (Postgres TIMESTAMPTZ)
  merged against tz-naive macro dates (macro_indicators.date), and the
  symmetric failure when the two sides carry *different* tz-aware zones.
- ValueError "X must not be empty" (hmm_model.classify_latest) after
  ``dropna(subset=feature_cols)`` annihilated every row because a macro
  column had zero non-NaN overlap with the price window.
- Unbounded stale carry-forward of macro values after a feed stalls.

Pure-function tests: no database required.
"""

import logging

import numpy as np
import pandas as pd
import pytest

from app.lab.regime.features import build_regime_features

FEATURES_LOGGER = "app.lab.regime.features"


def _prices(n: int = 120, tz: str | None = "UTC", start: str = "2024-01-01") -> pd.DataFrame:
    """Deterministic synthetic daily price frame with ts/close columns."""
    dates = pd.date_range(start, periods=n, freq="D", tz=tz)
    rng = np.random.default_rng(7)
    close = 100.0 * np.cumprod(1 + rng.normal(0.0005, 0.01, n))
    return pd.DataFrame({"ts": dates, "close": close})


def test_tz_aware_prices_with_naive_macro_dates_merge_and_normalize():
    """tz-aware price ts + tz-naive macro dates must merge without MergeError,
    and BOTH sides must be normalized to tz-naive UTC so Postgres TIMESTAMPTZ
    and SQLite behave identically."""
    prices = _prices(tz="UTC")
    macro = pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=100, freq="D"),  # tz-naive
            "vix": np.linspace(15.0, 25.0, 100),
            "yield_slope": np.linspace(0.8, 0.2, 100),
            "credit_spread": np.linspace(2.5, 1.8, 100),
        }
    )

    features = build_regime_features(prices, macro)

    assert len(features) > 0
    # Merge succeeded and every macro value survived intact.
    assert not features[["vix", "yield_slope", "credit_spread"]].isna().any().any()
    assert features["vix"].iloc[-1] == pytest.approx(25.0)
    # Both sides normalized to tz-naive UTC before the merge -> naive index.
    assert features.index.tz is None
    # Column order convention preserved for persisted-model scaler mapping.
    assert list(features.columns) == [
        "ret",
        "vol",
        "drawdown",
        "vix",
        "yield_slope",
        "credit_spread",
    ]


def test_mismatched_tz_aware_sides_do_not_raise():
    """Price ts in UTC and macro dates in another aware zone must still merge
    (normalize both sides identically instead of localizing one to the other)."""
    prices = _prices(tz="UTC")
    macro = pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=100, freq="D", tz="America/New_York"),
            "vix": np.linspace(15.0, 25.0, 100),
        }
    )

    features = build_regime_features(prices, macro)  # must not raise MergeError

    assert len(features) > 0
    assert not features["vix"].isna().any()


def test_macro_entirely_outside_price_range_returns_price_only_features(caplog):
    """A macro column with zero overlap must be excluded from feature_cols so
    dropna cannot annihilate every row; base features must survive."""
    prices = _prices(n=120, tz="UTC")  # 2024-01-01 .. 2024-04-29
    macro = pd.DataFrame(
        {
            "ts": pd.date_range("2025-01-01", periods=30, freq="D"),  # entirely after
            "vix": np.linspace(15.0, 25.0, 30),
        }
    )

    with caplog.at_level(logging.WARNING, logger=FEATURES_LOGGER):
        features = build_regime_features(prices, macro)

    # Price-only features survive instead of an all-NaN annihilated frame.
    assert len(features) > 0
    assert list(features.columns) == ["ret", "vol", "drawdown"]
    assert not features.isna().any().any()
    # The excluded column is logged.
    assert any(
        record.levelno == logging.WARNING and "vix" in record.getMessage()
        for record in caplog.records
    )


def test_partial_vix_overlap_caps_stale_carry_forward():
    """VIX observed only over the first 30 days of a 120-day price window:
    values may be carried forward at most 10 price rows past the last
    observation; rows beyond that are stale and dropped, but the frame itself
    stays non-empty with complete rows inside the cap window."""
    prices = _prices(n=120, tz="UTC")
    macro = pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=30, freq="D"),
            "vix": np.linspace(15.0, 18.0, 30),
        }
    )

    features = build_regime_features(prices, macro)

    assert len(features) > 0
    assert "vix" in features.columns
    assert not features.isna().any().any()

    # Cap: frame ends within 10 price rows (days here) of the last observation
    # instead of running stale to the end of the price window.
    last_macro_date = macro["ts"].max()  # 2024-01-30
    tail_age_days = (features.index.max() - last_macro_date).days
    assert tail_age_days <= 10

    # Rows inside the cap window carry the last observed value forward.
    assert features["vix"].iloc[-1] == pytest.approx(18.0)


def test_a_lagging_macro_series_does_not_drop_the_newest_price_rows():
    """Prod 2026-09-21..28: BAA10Y/T10Y2Y had values up to Sep 24 but FRED's
    VIX stopped at Sep 17. The wide-frame merge matched Sep 18's row, whose
    vix was NaN, so every newer price row was dropped and the regime
    classified Sep 17 for a week. Each series now carries forward on its own."""
    prices = _prices(n=60, tz="UTC", start="2026-08-01")
    dates = pd.date_range("2026-08-01", periods=60, freq="D")
    macro = pd.DataFrame(
        {
            "ts": dates,
            "vix": [15.0 + i * 0.01 if i < 55 else np.nan for i in range(60)],
            "yield_slope": np.linspace(0.4, 0.3, 60),
            "credit_spread": np.linspace(1.6, 1.4, 60),
        }
    )

    features = build_regime_features(prices, macro)

    assert features.index.max() == prices["ts"].max().tz_localize(None)
    # The newest rows carry VIX's last observation; the other series keep their own.
    assert features["vix"].iloc[-1] == pytest.approx(15.0 + 54 * 0.01)
    assert features["credit_spread"].iloc[-1] == pytest.approx(1.4)
