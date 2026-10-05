"""Feature engineering: ta indicators, lagged returns, calendar features."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regime features — encoding regime state for ML models
# ---------------------------------------------------------------------------

# Regime label → numeric index for one-hot encoding.
_REGIME_LABEL_INDEX: dict[str, int] = {
    "bull": 0, "bear": 1, "high_vol": 2, "low_vol": 3,
    "sideways": 4, "transition": 5, "neutral": 6,
}

#: Mirrors app.lab.regime.gate.CRISIS_VIX_THRESHOLD (30.0) — duplicated
#: rather than imported to keep quant_ml decoupled from app.lab.regime
#: (see build_features' regime_snapshot docstring).
_CRISIS_VIX_THRESHOLD = 30.0

#: Longest window build_features uses (the z-score baseline). Shorter series
#: shrink it, and the 63-day windows, to fit -- so a model trained on a full
#: history must be served at least ``LONG_WINDOW + 1`` bars, or the columns
#: it predicts on are not the columns it was fitted on.
LONG_WINDOW = 252


def build_regime_ml_features(
    regime_label: str,
    confidence: float | None = None,
    crisis: bool = False,
) -> dict[str, float]:
    """Encode regime state as numeric features for ML models.

    Returns a flat dict with:
      - regime_bull .. regime_neutral: one-hot encoding of regime label
      - regime_confidence: scalar confidence in [0, 1]
      - regime_crisis: 1.0 if crisis, else 0.0
    """
    idx = _REGIME_LABEL_INDEX.get(regime_label, _REGIME_LABEL_INDEX["neutral"])
    features: dict[str, float] = {}
    for name, i in _REGIME_LABEL_INDEX.items():
        features[f"regime_{name}"] = 1.0 if i == idx else 0.0
    features["regime_confidence"] = confidence if confidence is not None else 0.5
    features["regime_crisis"] = 1.0 if crisis else 0.0
    return features


def augment_with_regime_features(
    feature_df: Any,
    regime_label: str,
    confidence: float | None = None,
    crisis: bool = False,
) -> Any:
    """Add regime columns to an existing feature DataFrame.

    Args:
        feature_df: pandas DataFrame of features (one row per observation).
        regime_label: Current regime label.
        confidence: Regime classifier confidence.
        crisis: Whether crisis conditions are active.

    Returns the DataFrame with additional regime_* columns appended.

    .. warning::
       **Not currently wired into any training or prediction path**, and it
       should not be re-wired in this form. Every regime column is assigned as a
       *scalar broadcast over all rows* (``feature_df[col] = val``), so a single
       snapshot — today's regime — is stamped onto the whole historical window.
       At fit time that is a zero-variance column carrying no information; at
       predict time it is a *different* constant, which is train/serve skew.
       Track D1a (5fef637) wired this into ``build_features``; the 2026-08-28
       audit sweep (67ef9a8) removed the call sites, correctly, under its
       look-ahead-bias heading.

       To make regime conditioning real, the snapshot must be resolved
       *per-row* from a point-in-time regime history (the regime as known on
       each feature row's own date), not from one current snapshot. Until then
       ``build_features``' ``regime_snapshot`` parameter stays accepted-but-
       unused by callers, deliberately.
    """
    regime_feats = build_regime_ml_features(regime_label, confidence, crisis)
    for col, val in regime_feats.items():
        feature_df[col] = val
    return feature_df


def build_features(
    prices: list[float],
    dates: list[str] | None = None,
    lags: int = 5,
    use_ta: bool = True,
    regime_snapshot: dict[str, Any] | None = None,
    extra_features: Any | None = None,
) -> Any:
    """Build a feature matrix from a price series.

    Includes lagged daily returns, rolling statistics, and optional ta
    technical indicators (RSI, MACD, Bollinger). Window sizes adapt to the
    available data length so short series don't produce empty output.

    ``extra_features``, when given, is a DataFrame indexed by the same
    dates (e.g. from ``app.foundation.data_engineering.pit_panel_joins
    .pit_ml_features_for_symbol``) joined on per-row, by date -- never a
    scalar broadcast across every row. See ``augment_with_regime_features``'s
    docstring above for why that distinction matters here specifically.
    """
    import pandas as pd

    idx = pd.to_datetime(dates) if dates else range(len(prices))
    close = pd.Series(prices, index=idx, dtype=float)
    returns = close.pct_change()
    n = len(returns)

    df = pd.DataFrame(index=close.index)

    # Lagged returns
    for lag in range(1, lags + 1):
        df[f"ret_lag_{lag}"] = returns.shift(lag)

    # Rolling statistics — cap windows at data length
    for window in (5, 21, 63):
        w = min(window, n - 1)
        if w >= 2:
            df[f"vol_{window}d"] = returns.rolling(w).std()
            df[f"ret_{window}d"] = returns.rolling(w).mean()

    # Z-score vs long-term baseline — only when enough data
    long_w = min(LONG_WINDOW, n - 1)
    short_w = min(21, long_w - 1)
    if long_w > short_w:
        df["zscore_21_long"] = (
            returns.rolling(short_w).mean() - returns.rolling(long_w).mean()
        ) / (returns.rolling(long_w).std() + 1e-9)

    # Calendar features
    if isinstance(close.index, pd.DatetimeIndex):
        idx_series = close.index.to_series()
        df["day_of_week"] = idx_series.dt.dayofweek
        df["month"] = idx_series.dt.month

    # ta indicators
    if use_ta:
        try:
            from ta.momentum import RSIIndicator
            from ta.trend import MACD
            from ta.volatility import BollingerBands

            rsi_w = min(14, n - 1)
            df["rsi_14"] = RSIIndicator(close, window=rsi_w).rsi()
            macd_obj = MACD(close)
            df["macd"] = macd_obj.macd()
            df["macd_signal"] = macd_obj.macd_signal()
            bb_w = min(20, n - 1)
            bb_obj = BollingerBands(close, window=bb_w)
            df["bb_pct"] = bb_obj.bollinger_pband()
        except Exception:
            pass

    if regime_snapshot is not None:
        try:
            vix = regime_snapshot.get("vix")
            crisis = isinstance(vix, (int, float)) and vix > _CRISIS_VIX_THRESHOLD
            df = augment_with_regime_features(
                df,
                regime_label=regime_snapshot.get("label", "neutral"),
                confidence=regime_snapshot.get("confidence"),
                crisis=crisis,
            )
        except Exception:
            logger.exception("build_features: regime augmentation failed, continuing without it")

    # Only the price-derived columns built above are required to be
    # non-NaN per row -- they're reliably present once the rolling/ta
    # warmup window has passed. extra_features is joined AFTER this point,
    # specifically so its NaN (e.g. IBES SUE, populated only on
    # earnings-announcement dates -- see pit_panel_joins' docstrings) never
    # drops an otherwise-valid row here. It's imputed downstream in the
    # sklearn Pipeline instead (see build_pipeline), where the imputer's
    # fitted state is train/serve-symmetric -- an ad hoc fillna computed
    # fresh on a single live predict-time row would not be.
    required_cols = list(df.columns)
    df = df.dropna(subset=required_cols)

    if extra_features is not None:
        df = df.join(extra_features, how="left")

    return df
