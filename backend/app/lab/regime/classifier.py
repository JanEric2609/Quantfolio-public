"""Regime classifier orchestrator: ties HMM, features, and crisis gate together."""

from datetime import datetime, timezone, timedelta
import logging
from typing import Any, cast

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.lab.regime.features import build_regime_features
from app.lab.regime.hmm_model import RegimeHMM
from app.lab.regime.jump_model import JumpRegimeModel
from app.lab.regime.crisis_gate import evaluate_crisis
from app.lab.regime.history import record_regime_label
from app.foundation.data_backbone.bars import BarStore
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.data_backbone.regime_store import RegimeStore
from app.foundation.settings import get_public_settings
from app.foundation.models.entities import MacroIndicator

logger = logging.getLogger(__name__)

# Mapping from MacroIndicator.name to feature column name
MACRO_SERIES_MAP = {
    "VIXCLS": "vix",                    # CBOE volatility index
    "T10Y2Y": "yield_slope",            # 10Y-2Y treasury slope
    "BAA10Y": "credit_spread",          # Moody's BAA - 10Y treasury
}

# Minimum clean (finite) feature rows classify_and_store requires before it
# will trust the persisted model's output on them.
MIN_CLEAN_ROWS_CLASSIFY = 30

# The regime model (ADR 0004 amendment, 2026-09-28): the jump model, with the
# HMM kept fitted as its fallback. Settings: regime_model_kind ("jump" |
# "hmm"), regime_jump_penalty, regime_jump_model_id; regime_model_id stays
# the HMM's artefact id.
DEFAULT_MODEL_KIND = "jump"
DEFAULT_JUMP_PENALTY = 10.0
DEFAULT_JUMP_MODEL_ID = "regime_jump"

RegimeModel = RegimeHMM | JumpRegimeModel


def _load_regime_model(settings: dict[str, Any]) -> tuple[RegimeModel, str]:
    """The configured model and its snapshot ``source``, else the HMM.

    Falls back to the HMM when the jump model's package or artefact is
    missing, so a deploy without ``jumpmodels`` keeps classifying.
    Raises when neither model can be loaded.
    """
    hmm_id = settings.get("regime_model_id", "regime_hmm")
    if settings.get("regime_model_kind", DEFAULT_MODEL_KIND) == "jump":
        jump_id = settings.get("regime_jump_model_id", DEFAULT_JUMP_MODEL_ID)
        try:
            return JumpRegimeModel.load(jump_id), "jump"
        except Exception as exc:
            logger.warning("Classifier: jump model %s unavailable (%s); using the HMM", jump_id, exc)
    return RegimeHMM.load(hmm_id), "hmm"

# A feature column whose standard deviation falls below this is treated as
# constant: StandardScaler divides by ~zero, producing NaN scaled features and
# degenerate posteriors (the frozen {sideways: 0.99994} production failure).
CONSTANT_STD_THRESHOLD = 1e-12


def _validate_feature_matrix(features: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Drop rows containing NaN/±inf values and flag constant feature columns.

    Numerical input hygiene shared by classify_and_store and refit_regime_model:
    a single NaN or inf row can poison StandardScaler/GaussianHMM into NaN
    features and bit-identical degenerate posteriors, so offending rows are
    removed up front, and constant columns are surfaced for the caller to
    refuse on before fitting.

    Args:
        features: Feature frame as produced by build_regime_features.

    Returns:
        Tuple of (cleaned frame with offending rows dropped, report dict):
        report["dropped"] is the number of removed rows; report
        ["constant_columns"] lists numeric columns whose std on the cleaned
        frame is below CONSTANT_STD_THRESHOLD.
    """
    report: dict[str, Any] = {"dropped": 0, "constant_columns": []}
    if features is None or len(features) == 0:
        return features, report

    numeric = features.select_dtypes(include=[np.number])
    if numeric.shape[1] == 0:
        return features, report

    finite_mask = np.isfinite(numeric.to_numpy(dtype=float)).all(axis=1)
    cleaned = cast(pd.DataFrame, features.loc[finite_mask].copy())
    report["dropped"] = int((~finite_mask).sum())

    if len(cleaned) > 1:
        # numpy-side std: pandas stubs type DataFrame.std ambiguously across
        # overloads, and a scalar-typed result would break column indexing.
        values = cleaned[numeric.columns].to_numpy(dtype=float)
        stds = values.std(axis=0, ddof=0)
        report["constant_columns"] = [
            col for col, s in zip(numeric.columns, stds) if float(s) < CONSTANT_STD_THRESHOLD
        ]

    return cleaned, report


def _newest_bar_age_days(bars_df: pd.DataFrame, ts: datetime) -> tuple[float, datetime]:
    """Age in whole-day fractions of the newest bar relative to ts, and its UTC timestamp."""
    last_ts = pd.to_datetime(bars_df["ts"].max())
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize(timezone.utc)
    else:
        last_ts = last_ts.tz_convert(timezone.utc)
    age_days = (ts - last_ts.to_pydatetime()).total_seconds() / 86400.0
    return age_days, last_ts.to_pydatetime()


def _attempt_index_bar_refresh(
    db: Session,
    *,
    symbol: str,
    end: datetime,
    lookback_days: int,
) -> None:
    """One bounded backfill of the index symbol via the shared ingestion entrypoint.

    Never raises into cron: any failure is logged and left for the caller's
    still-stale skip-write.
    """
    try:
        start_iso = (end - timedelta(days=lookback_days)).date().isoformat()
        end_iso = end.date().isoformat()
        logger.warning(
            "Regime guard: newest %s bar older than regime_max_bar_age_days; "
            "attempting one backfill (%s .. %s)",
            symbol, start_iso, end_iso,
        )
        DataIngester(db).ingest_bar_prices(symbol=symbol, start_date=start_iso, end_date=end_iso)
    except Exception as e:
        logger.warning("Regime guard: index-bar refresh failed for %s: %s", symbol, e)


def _ensure_fresh_index_bars(
    db: Session,
    *,
    symbol: str,
    bars_df: pd.DataFrame,
    end: datetime,
    lookback_days: int,
    max_age_days: float,
) -> pd.DataFrame | None:
    """Return bars fresh enough to classify on, or None when persistently stale.

    On staleness, attempts exactly one backfill of `symbol` and re-reads; a
    second stale read returns None so the caller skips writing.
    """
    age_days, _ = _newest_bar_age_days(bars_df, end)
    if age_days <= max_age_days:
        return bars_df

    _attempt_index_bar_refresh(db, symbol=symbol, end=end, lookback_days=lookback_days)

    refreshed = BarStore(db).get_bars(
        symbol=symbol, start=end - timedelta(days=lookback_days), end=end,
    )
    if refreshed is None or len(refreshed) == 0:
        return None
    refreshed_age_days, _ = _newest_bar_age_days(refreshed, end)
    if refreshed_age_days > max_age_days:
        return None
    return refreshed


def classify_and_store(
    db: Session,
    *,
    now: datetime | None = None,
    lookback_days: int = 400,
) -> dict:
    """Build features, classify the latest regime via the persisted HMM,
    evaluate the crisis gate, and write one regime_snapshots row.

    Args:
        db: Database session.
        now: Timestamp for the snapshot (defaults to now UTC). Used for testing.
        lookback_days: Number of days to look back for price data (default 400).

    Returns:
        Dict with keys:
            - "ts": timestamp of the snapshot
            - "label": regime label ("bull", "sideways", or "bear")
            - "score": float in [0, 1]. For the jump model, the calibrated
              probability that today's label survives hindsight
              (``JumpRegimeModel.classify_latest``); for the HMM, its
              max posterior probability
            - "crisis": bool, whether crisis conditions are met
            - "probs": dict {label: float}, per-label probabilities
            - "written": bool, whether the snapshot was actually written to DB
            - "dropped_nonfinite_rows": int, feature rows removed by the
              NaN/±inf hygiene pass
            - "reason" (optional): explanation if written=False
    """
    # Look-ahead invariant: correctness relies on the scheduled 07:15 UTC
    # cron (regime/jobs.py::register_regime_daily_job) running well before
    # the current trading day's bar exists — at that hour the US market
    # hasn't opened yet, so the newest bar loaded below is always
    # yesterday's completed close, never today's in-progress one. Nothing
    # here enforces that ordering; it holds only because of the cron
    # schedule (07:15, after price_backfill_daily at 06:30).
    ts = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "ts": ts,
        "written": False,
    }

    try:
        # 1. Read settings
        settings = get_public_settings(db)
        model_id = settings.get("regime_model_id", "regime_hmm")
        index_symbol = settings.get("regime_index_symbol", "^STOXX50E")
        vix_threshold = settings.get("regime_crisis_vix_threshold", 30.0)
        credit_spread_threshold = settings.get("regime_crisis_credit_spread_threshold", 3.0)
        drawdown_threshold = settings.get("regime_crisis_drawdown_threshold", -0.15)

        logger.debug(
            f"Classifier settings: model={model_id}, symbol={index_symbol}, "
            f"vix_th={vix_threshold}, cs_th={credit_spread_threshold}, dd_th={drawdown_threshold}"
        )

        # 2. Load price bars
        bar_store = BarStore(db)
        start_date = ts - timedelta(days=lookback_days)
        bars_df = bar_store.get_bars(symbol=index_symbol, start=start_date, end=ts)

        if bars_df is None or len(bars_df) == 0:
            result["reason"] = f"no bars available for {index_symbol}"
            logger.warning(f"Classifier: {result['reason']}")
            return result

        logger.debug(f"Loaded {len(bars_df)} bars for {index_symbol}")

        # 2b. Freshness guard: refuse to classify on stale index slices
        max_age_days = float(settings.get("regime_max_bar_age_days", 7))
        _, last_bar_ts = _newest_bar_age_days(bars_df, ts)
        fresh_bars = _ensure_fresh_index_bars(
            db,
            symbol=index_symbol,
            bars_df=bars_df,
            end=ts,
            lookback_days=lookback_days,
            max_age_days=max_age_days,
        )
        if fresh_bars is None:
            result["stale_bars"] = True
            result["last_bar_ts"] = last_bar_ts
            result["reason"] = "stale_bars"
            logger.warning(
                "Classifier: refusing to write regime snapshot — %s bars stale "
                "(last_bar_ts=%s, older than %.1f days)",
                index_symbol, last_bar_ts, max_age_days,
            )
            return result
        bars_df = fresh_bars
        result["stale_bars"] = False
        _, last_bar_ts = _newest_bar_age_days(bars_df, ts)
        result["last_bar_ts"] = last_bar_ts

        # 3. Build macro frame from macro_indicators table
        macro_df = _build_macro_frame(db, start_date, ts)
        if macro_df is not None:
            logger.debug(f"Macro frame shape: {macro_df.shape}, columns: {list(macro_df.columns)}")

        # 4. Build features
        features = build_regime_features(
            cast(pd.DataFrame, bars_df[["ts", "close"]]),
            macro_df=macro_df,
            vol_window=21,
        )
        logger.debug(f"Built features: shape {features.shape}, columns {list(features.columns)}")

        # 4b. Numerical hygiene: drop NaN/±inf rows, refuse when too few remain
        features, hygiene = _validate_feature_matrix(features)
        result["dropped_nonfinite_rows"] = hygiene["dropped"]
        if hygiene["dropped"]:
            logger.warning(
                "Classifier: dropped %d non-finite feature rows", hygiene["dropped"]
            )
        if len(features) < MIN_CLEAN_ROWS_CLASSIFY:
            result["reason"] = "insufficient_clean_features"
            logger.warning(
                "Classifier: refusing to classify — only %d clean feature rows "
                "(need at least %d)",
                len(features), MIN_CLEAN_ROWS_CLASSIFY,
            )
            return result

        # 4c. The row classified is the last complete feature row, which a
        # stalled macro series can hold days behind the newest bar. Record
        # it, and refuse when it is as stale as the bars would have to be.
        feature_ts = pd.Timestamp(cast(Any, features.index[-1])).tz_localize(timezone.utc)
        feature_dt = cast(datetime, feature_ts.to_pydatetime())
        result["feature_ts"] = feature_dt
        feature_lag_days = (last_bar_ts - feature_dt).total_seconds() / 86400.0
        if feature_lag_days > max_age_days:
            result["reason"] = "stale_features"
            logger.warning(
                "Classifier: refusing to write regime snapshot — newest complete "
                "feature row %s is %.1f days behind the newest bar %s",
                feature_ts.date(), feature_lag_days, last_bar_ts.date(),
            )
            return result

        # 5. Load the persisted model (jump model, else the HMM fallback)
        try:
            model, source = _load_regime_model(settings)
            logger.debug("Loaded %s regime model", source)
        except Exception as e:
            result["reason"] = f"model_unavailable: {str(e)}"
            logger.warning(f"Classifier: {result['reason']}")
            return result
        result["source"] = source

        # 6. Classify the latest row
        classification = model.classify_latest(features)
        result["label"] = classification["label"]
        result["score"] = classification["score"]
        result["probs"] = classification["probs"]
        if "score_basis" in classification:
            result["score_basis"] = classification["score_basis"]

        logger.debug(f"Classification: label={result['label']}, score={result['score']:.4f}")

        # 7. Compute current crisis inputs
        drawdown_value = float(features["drawdown"].iloc[-1]) if "drawdown" in features.columns else None
        vix_value = float(features["vix"].iloc[-1]) if "vix" in features.columns else None
        credit_spread_value = float(features["credit_spread"].iloc[-1]) if "credit_spread" in features.columns else None

        crisis_eval = evaluate_crisis(
            vix=vix_value,
            credit_spread=credit_spread_value,
            drawdown=drawdown_value,
            vix_threshold=vix_threshold,
            credit_spread_threshold=credit_spread_threshold,
            drawdown_threshold=drawdown_threshold,
        )
        result["crisis"] = crisis_eval["crisis"]

        logger.debug(
            f"Crisis gate: crisis={result['crisis']}, triggers={crisis_eval['triggers']}, "
            f"vix={vix_value}, cs={credit_spread_value}, dd={drawdown_value}"
        )

        # 8. Write snapshot to DB
        payload = {
            "crisis": result["crisis"],
            "triggers": crisis_eval["triggers"],
            "probs": result["probs"],
            "score_basis": result.get("score_basis", "posterior"),
            "stale_bars": False,
            "last_bar_ts": last_bar_ts.isoformat(),
            "feature_ts": feature_dt.isoformat(),
            "features": {
                "drawdown": drawdown_value,
                "vix": vix_value,
                "credit_spread": credit_spread_value,
            },
        }

        regime_store = RegimeStore(db)
        success = regime_store.write_snapshot(
            ts=ts,
            label=result["label"],
            score=result["score"],
            source=source,
            payload=payload,
        )

        if success:
            result["written"] = True
            logger.info(f"Wrote regime snapshot: label={result['label']}, crisis={result['crisis']}")
            # Frozen daily call for the trust ledger; best-effort by design.
            record_regime_label(
                db,
                as_of=ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date(),
                label=result["label"],
                probs=result["probs"],
                model=source,
            )
        else:
            result["reason"] = "write_failed"
            logger.error("Failed to write regime snapshot")

    except Exception as e:
        result["reason"] = f"exception: {str(e)}"
        logger.exception(f"Unexpected error in classifier: {e}")

    return result


# Stored index bars that start this many days after the window start are
# backfilled once. Prod held ^STOXX50E only from 2025-08-11 (282 bars), so a
# "750-day" refit fitted three regimes on one calm year.
_HISTORY_GAP_DAYS = 45


def _ensure_index_history(
    db: Session,
    symbol: str,
    bars_df: pd.DataFrame,
    *,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Backfill the index once when stored bars start well after *start*."""
    first = pd.Timestamp(cast(Any, bars_df["ts"].min()))
    first = first.tz_localize(timezone.utc) if first.tzinfo is None else first.tz_convert(timezone.utc)
    if (first.to_pydatetime() - start).days <= _HISTORY_GAP_DAYS:
        return bars_df
    try:
        logger.warning(
            "Refit: %s bars start %s, %d days into the window; backfilling from %s",
            symbol, first.date(), (first.to_pydatetime() - start).days, start.date(),
        )
        DataIngester(db).ingest_bar_prices(
            symbol=symbol, start_date=start.date().isoformat(), end_date=end.date().isoformat(),
        )
        refreshed = BarStore(db).get_bars(symbol=symbol, start=start, end=end)
    except Exception as exc:
        logger.warning("Refit: history backfill for %s failed: %s", symbol, exc)
        return bars_df
    if refreshed is None or len(refreshed) <= len(bars_df):
        return bars_df
    return refreshed


def _refit_jump_model(
    settings: dict[str, Any],
    features: pd.DataFrame,
    *,
    base: str | None = None,
) -> dict[str, Any]:
    """Fit and save the jump model; never raises (the HMM refit continues)."""
    jump_id = settings.get("regime_jump_model_id", DEFAULT_JUMP_MODEL_ID)
    out: dict[str, Any] = {"saved": False, "model_id": jump_id}
    try:
        penalty = float(settings.get("regime_jump_penalty", DEFAULT_JUMP_PENALTY))
        model = JumpRegimeModel(n_states=3, jump_penalty=penalty)
        model.fit(features)
        if base is not None:
            model.save(jump_id, base=base)
        else:
            model.save(jump_id)
        out["saved"] = True
        out["jump_penalty"] = penalty
        logger.info("Refit: saved jump model %s (penalty %.1f, %d rows)", jump_id, penalty, len(features))
    except Exception as exc:
        out["reason"] = str(exc)
        logger.warning("Refit: jump model %s not refitted: %s", jump_id, exc)
    return out


def refit_regime_model(
    db: Session,
    *,
    base: str | None = None,
    lookback_days: int = 750,
    min_rows: int = 120,
) -> dict:
    """Fit fresh regime models on recent history and persist them.

    Reads settings for regime_model_id / regime_index_symbol, loads bars via
    BarStore (backfilling the index once when they cover much less than the
    window), builds macro frame via _build_macro_frame, builds features via
    build_regime_features, then fits and saves the jump model (the live
    model, see _refit_jump_model) and RegimeHMM(n_states=3) (its fallback,
    saved via model.save(model_id, base=base)).

    Returns:
        {"saved": bool (the HMM), "model_id": str, "n_rows": int,
         "dropped_nonfinite_rows": int, "jump": {"saved": bool, ...},
         "reason"?: str, "constant_columns"?: list[str]}.

    Defensive: if bars missing, fewer than min_rows clean feature rows remain
    after the NaN/±inf hygiene pass, any feature column is constant, or EM
    does not converge, return {"saved": False, "reason": ...} WITHOUT raising
    and without overwriting the persisted model. Catches unexpected
    exceptions and returns saved=False with the reason.
    """
    result: dict[str, Any] = {"saved": False}

    try:
        # 1. Read settings
        settings = get_public_settings(db)
        model_id = settings.get("regime_model_id", "regime_hmm")
        index_symbol = settings.get("regime_index_symbol", "^STOXX50E")

        result["model_id"] = model_id

        # 2. Load price bars
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)
        bar_store = BarStore(db)
        bars_df = bar_store.get_bars(symbol=index_symbol, start=start, end=now)

        if bars_df is None or len(bars_df) == 0:
            result["reason"] = f"no bars available for {index_symbol}"
            logger.warning(f"Refit: {result['reason']}")
            return result

        logger.debug(f"Refit: loaded {len(bars_df)} bars for {index_symbol}")
        bars_df = _ensure_index_history(db, index_symbol, bars_df, start=start, end=now)
        result["first_bar_ts"] = str(bars_df["ts"].min())

        # 2b. Freshness guard: refuse fitting windows ending on stale bars
        max_age_days = float(settings.get("regime_max_bar_age_days", 7))
        _, last_bar_ts = _newest_bar_age_days(bars_df, now)
        fresh_bars = _ensure_fresh_index_bars(
            db,
            symbol=index_symbol,
            bars_df=bars_df,
            end=now,
            lookback_days=lookback_days,
            max_age_days=max_age_days,
        )
        if fresh_bars is None:
            result["last_bar_ts"] = last_bar_ts
            result["reason"] = "stale_window"
            logger.warning(
                "Refit: refusing fitting window — %s bars stale "
                "(last_bar_ts=%s, older than %.1f days)",
                index_symbol, last_bar_ts, max_age_days,
            )
            return result
        bars_df = fresh_bars

        # 3. Build macro frame — same path as classify_and_store
        macro_df = _build_macro_frame(db, start, now)
        if macro_df is not None:
            logger.debug(f"Refit: macro frame shape {macro_df.shape}")

        # 4. Build features using the SAME path as classify_and_store
        features = build_regime_features(
            cast(pd.DataFrame, bars_df[["ts", "close"]]),
            macro_df=macro_df,
            vol_window=21,
        )
        logger.debug(f"Refit: features shape {features.shape}")

        # 4b. Numerical hygiene: drop NaN/±inf rows before counting or fitting
        features, hygiene = _validate_feature_matrix(features)
        result["dropped_nonfinite_rows"] = hygiene["dropped"]
        if hygiene["dropped"]:
            logger.warning(
                "Refit: dropped %d non-finite feature rows", hygiene["dropped"]
            )

        n_rows = len(features)
        result["n_rows"] = n_rows

        if n_rows < min_rows:
            result["reason"] = f"only {n_rows} clean feature rows, need at least {min_rows}"
            logger.warning(f"Refit: {result['reason']}")
            return result

        # 4c. Degenerate-variance guard: a constant column drives StandardScaler
        # to divide by ~zero, yielding NaN scaled features and degenerate
        # posteriors — never fit on one.
        if hygiene["constant_columns"]:
            result["reason"] = "degenerate_features"
            result["constant_columns"] = hygiene["constant_columns"]
            logger.warning(
                "Refit: refusing to fit — constant feature columns %s "
                "(std < %.0e; StandardScaler would divide by ~zero)",
                hygiene["constant_columns"], CONSTANT_STD_THRESHOLD,
            )
            return result

        # 5. The jump model first, so an HMM that fails its convergence guard
        # below cannot keep it stale.
        result["jump"] = _refit_jump_model(settings, features, base=base)

        # 5a. The HMM: the fallback when the jump model cannot be loaded.
        model = RegimeHMM(n_states=3)
        model.fit(features)
        logger.debug("Refit: model fitted")

        # 5b. Convergence guard: never overwrite a good persisted model with
        # one whose EM did not converge (unreliable posteriors).
        if not model.converged:
            result["reason"] = "not_converged"
            logger.warning(
                "Refit: EM did not converge within n_iter=%d; "
                "refusing to overwrite persisted model %s",
                model.n_iter, model_id,
            )
            return result

        # 6. Persist — only pass base when it is explicitly provided
        if base is not None:
            model.save(model_id, base=base)
        else:
            model.save(model_id)

        result["saved"] = True
        logger.info(f"Refit: saved model {model_id} ({n_rows} rows)")

    except Exception as e:
        result["reason"] = f"exception: {str(e)}"
        logger.exception(f"Unexpected error in refit_regime_model: {e}")

    return result


def _build_macro_frame(
    db: Session,
    start: datetime,
    end: datetime,
) -> pd.DataFrame | None:
    """Build macro indicator dataframe from macro_indicators table.

    Queries rows in the date range, maps series names to columns per MACRO_SERIES_MAP,
    and pivots to wide form.

    Args:
        db: Database session.
        start: Start datetime (will convert to date).
        end: End datetime (will convert to date).

    Returns:
        DataFrame with columns ts (datetime) + any of [vix, yield_slope, credit_spread]
        that have data, or None if no rows found.
    """
    try:
        start_date = start.date() if isinstance(start, datetime) else start
        end_date = end.date() if isinstance(end, datetime) else end

        # Query all macro indicators in the date range
        rows = db.query(MacroIndicator).filter(
            MacroIndicator.date >= start_date,
            MacroIndicator.date <= end_date,
        ).all()

        if not rows:
            return None

        # Build a list of dicts: {ts, column_name, value}
        data = []
        for row in rows:
            if row.name in MACRO_SERIES_MAP:
                col_name = MACRO_SERIES_MAP[row.name]
                # Tz-naive here; build_regime_features() aligns tz-awareness
                # against the price frame before merging (BarStore's `ts` is
                # tz-aware on Postgres/TIMESTAMPTZ but tz-naive on SQLite).
                ts = pd.Timestamp(row.date)
                data.append({
                    "ts": ts,
                    col_name: float(row.value),
                })

        if not data:
            return None

        # Aggregate: group by ts, take the first value for each column
        # (in case multiple series on same date)
        df = pd.DataFrame(data)
        df = df.groupby("ts").first().reset_index()

        return df

    except Exception as e:
        logger.warning(f"Error building macro frame: {e}")
        return None
