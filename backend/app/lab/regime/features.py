"""Feature construction for regime classification: market indices and macro indicators."""
from __future__ import annotations

import logging
from typing import cast

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Canonical macro feature columns, in the order they are appended to the base
# features. Persisted RegimeHMM models store this column order (the fitted
# scaler maps by position), so it must not change.
MACRO_COLUMNS: tuple[str, ...] = ("vix", "yield_slope", "credit_spread")
BASE_FEATURE_COLUMNS: tuple[str, ...] = ("ret", "vol", "drawdown")

# A macro value may be carried forward over at most this many price rows
# (via merge_asof backward matching) before it is treated as stale/missing.
# Bounds the damage of a stalled feed (e.g. VIX stops publishing): without
# the cap one ancient value silently propagates across the whole frame.
MACRO_FFILL_LIMIT = 10


def _as_naive_utc(series: pd.Series) -> pd.Series:
    """Normalize a datetime-like Series to tz-naive UTC datetimes.

    Naive values pass through unchanged; tz-aware values are converted to UTC
    and stripped of their timezone. ``utc=True`` makes mixed-offset or
    object-dtype input uniform instead of raising, so price frames from
    Postgres TIMESTAMPTZ (tz-aware) and SQLite (tz-naive) take the identical
    path.
    """
    return cast(pd.Series, pd.to_datetime(series, utc=True)).dt.tz_localize(None)


def build_regime_features(
    prices_df: pd.DataFrame,
    macro_df: pd.DataFrame | None = None,
    *,
    vol_window: int = 21,
    drawdown_window: int = 63,
) -> pd.DataFrame:
    """Build regime classification features from price and macro data.

    Pure function: no database access, operates on pandas DataFrames.

    Args:
        prices_df: Daily market-index bars, ascending by `ts`. Must contain
            columns `ts` (datetime) and `close` (price). May contain other
            OHLCV columns which are ignored.
        macro_df: Optional macro indicators frame with a `ts` column plus
            any subset of {`vix`, `yield_slope`, `credit_spread`}. If provided,
            left-merged onto the feature frame using `pd.merge_asof` so the
            latest macro value at-or-before each price timestamp is used,
            carried forward for at most MACRO_FFILL_LIMIT price rows. If None,
            macro columns are absent.
        vol_window: Rolling window for volatility computation (default 21 days).
        drawdown_window: Rolling lookback for the peak `drawdown` is measured
            against (default 63 trading days, ~3 months).

    Returns:
        DataFrame indexed by ts (tz-naive UTC DatetimeIndex), with columns:
            - `ret`: daily pct change of `close` (close.pct_change())
            - `vol`: rolling standard deviation of `ret` over vol_window
            - `drawdown`: distance from the trailing drawdown_window-day peak
              (close / close.rolling(drawdown_window).max() - 1, always ≤ 0).
              A rolling peak is used instead of the all-time running max
              (close.cummax()) deliberately: a global running-peak drawdown
              drifts in one direction for the entire span of any regime that
              starts after the all-time high, which starves that regime's
              estimated HMM variance down to near-zero and produces an
              overconfident, sticky posterior (observed in production — see
              the regime engine audit). A rolling peak resets periodically
              instead, so the feature carries usable within-regime variance.
            - optionally: `vix`, `yield_slope`, `credit_spread` (each only if
              macro_df provided AND the column has at least one non-NaN value
              after the merge)

        Both merge sides are normalized to tz-naive UTC before merging, so the
        result is identical whether prices come from Postgres TIMESTAMPTZ or
        SQLite. Rows with NaN in any feature column are dropped (warmup period
        and stale-macro tail). A macro column with zero coverage is excluded
        instead of being allowed to annihilate every row. The base columns
        ret/vol/drawdown are always present, in canonical order followed by
        any surviving macro columns (persisted-model scaler mapping depends on
        this order).

    Raises:
        ValueError: if prices_df is missing required columns 'ts' or 'close'.
    """
    if "ts" not in prices_df.columns or "close" not in prices_df.columns:
        raise ValueError("prices_df must contain columns 'ts' and 'close'")

    # Ensure ts is datetime (normalized to tz-naive UTC) and sorted
    df = cast(pd.DataFrame, prices_df[["ts", "close"]].copy())
    df["ts"] = _as_naive_utc(cast(pd.Series, df["ts"]))
    df = df.sort_values("ts").reset_index(drop=True)

    # Compute market features
    df["ret"] = df["close"].pct_change()
    df["vol"] = df["ret"].rolling(window=vol_window).std()
    rolling_peak = df["close"].rolling(window=drawdown_window, min_periods=1).max()
    df["drawdown"] = df["close"] / rolling_peak - 1

    # Merge macro features if provided
    if macro_df is not None and len(macro_df) > 0:
        available_macro_cols = [
            col for col in MACRO_COLUMNS if col in macro_df.columns
        ]

        if available_macro_cols:
            # Ensure macro_df ts is datetime (same normalization as the price
            # side) and sorted, so merge_asof never sees mismatched dtypes.
            macro_copy = cast(
                pd.DataFrame, macro_df[["ts", *available_macro_cols]].copy()
            )
            macro_copy["ts"] = _as_naive_utc(cast(pd.Series, macro_copy["ts"]))
            macro_copy = macro_copy.sort_values("ts").reset_index(drop=True)

            # One merge per column, each over that column's own non-null
            # observations. A single merge_asof over the wide frame matches
            # whole rows: on a date where BAA10Y has a value but VIX does not
            # yet (FRED publishes VIX days later), the matched row's vix is
            # NaN and the price row is dropped — the regime then classified
            # the same old row for a week (2026-09-21..28).
            row_rank = np.arange(len(df))
            for col in available_macro_cols:
                observed = cast(
                    pd.DataFrame, macro_copy.loc[macro_copy[col].notna(), ["ts", col]]
                )
                observed = observed.assign(_match_ts=observed["ts"])
                df = pd.merge_asof(df, observed, on="ts", direction="backward")

                # Cap the carry-forward: null values whose matched observation
                # lies more than MACRO_FFILL_LIMIT price rows back. merge_asof
                # alone would propagate the last observation indefinitely once
                # a feed stalls.
                matched_rank = df["ts"].searchsorted(df["_match_ts"], side="right") - 1
                is_stale = df["_match_ts"].notna() & (
                    (row_rank - matched_rank) > MACRO_FFILL_LIMIT
                )
                df.loc[is_stale, col] = np.nan
                df = df.drop(columns="_match_ts")

    # Assemble feature columns. A macro column with zero non-NaN coverage is
    # EXCLUDED rather than passed to dropna(subset=...), which would annihilate
    # every row and crash the downstream classifier ("X must not be empty").
    feature_cols = list(BASE_FEATURE_COLUMNS)
    if macro_df is not None and len(macro_df) > 0:
        for col in MACRO_COLUMNS:
            if col not in df.columns:
                continue
            covered = int(df[col].notna().sum())
            if covered == 0:
                logger.warning(
                    "regime_features: macro column %s excluded, 0/%d rows "
                    "covered after merge (feed outside price range or empty)",
                    col,
                    len(df),
                )
                continue
            logger.info(
                "regime_features: macro column %s included, %d/%d rows covered",
                col,
                covered,
                len(df),
            )
            feature_cols.append(col)

    df = df.set_index("ts")
    df = df.dropna(subset=feature_cols)

    return cast(pd.DataFrame, df[feature_cols])
