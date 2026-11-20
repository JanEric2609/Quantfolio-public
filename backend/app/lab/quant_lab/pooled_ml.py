"""One pooled cross-sectional model for Discover's ml_signal (ADR 0015 #24).

ADR 0015 ruling #24 sets the prediction target: the forward
volatility-scaled return, cross-sectional. Until 2026-09-28 Discover
trained one triple-barrier classifier per shortlisted ticker on ~700 rows
of its own history. All 43 models on prod were rejected against the
majority-class baseline, and a replay found Cohen's kappa <= 0 for every
ticker: no skill. One model pooled over a cross-section sees ~150x the rows,
and a cross-sectional rank target removes the market's direction, which no
price-only model predicts (Gu, Kelly & Xiu 2020, RFS, on pooled models;
Moskowitz, Ooi & Pedersen 2012 on volatility scaling).

Everything is fixed here before any result was seen (the same discipline as
``lab.pooled_model.spec``, the monthly JKP study this complements; that one
cannot serve Discover because the JKP extract ends 2025-12):

- Universe: whatever close panel the caller passes (Discover passes the
  AlphaCrafter miner universe, Euro Stoxx 50 + S&P 100).
- Features (:data:`FEATURES`): price-only characteristics, each a centred
  rank within its date.
- Label: :func:`labels.vol_scaled_forward_return` over :data:`HORIZON`
  days, as a centred rank within its date.
- Models: ridge and LightGBM (:data:`MODELS`). LightGBM is served
  (:data:`SERVED_MODEL`); ridge is its reference.
- Validation: :func:`app.lab.quant_lab.cv.purged_embargo_splits` over dates,
  purge = :data:`HORIZON`. Each test date is scored only by a model fitted on
  earlier, purged dates.
- Gate: mean out-of-sample rank IC >= :data:`MIN_IC` and a Newey-West t
  (lags = :data:`HORIZON`, the label overlap) >= ``HLZ_T_THRESHOLD`` (3.0,
  Harvey, Liu & Zhu 2016, for newly mined signals).

Pure functions over DataFrames: this module fetches nothing; the caller
(``discover.jobs``/``discover.pipeline``) passes the close panel in. It sits
in quant_lab, not quant_ml, because it needs ``quant_metrics`` and
``cv.py``: a quant_ml edge to either closes the
``foundation -> lab.regime -> quant_ml.registry`` package cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np
import pandas as pd

HORIZON = 20
VOL_WINDOW = 60
MIN_IC = 0.02
N_FOLDS = 5
# A date's cross-section must hold at least this many names to be a
# training row or a scored test date.
MIN_CROSS_SECTION = 30
# Bars a symbol needs before its features are complete (momentum_12_1).
MIN_HISTORY = 253

FEATURES: tuple[str, ...] = (
    "ret_5d",          # short-term reversal (Jegadeesh 1990)
    "ret_21d",         # one-month reversal
    "ret_63d",
    "ret_126d",
    "mom_12_1",        # Jegadeesh & Titman (1993)
    "vol_21d",
    "vol_63d",         # low-volatility anomaly (Ang et al. 2006)
    "vol_ratio",       # volatility trend
    "max_ret_21d",     # lottery demand, MAX (Bali, Cakici & Whitelaw 2011)
    "dist_52w_high",   # George & Hwang (2004)
)

MODELS: tuple[str, ...] = ("ridge", "gbm")
SERVED_MODEL = "gbm"
RIDGE_ALPHA = 10.0
GBM_PARAMS: dict[str, Any] = {
    "n_estimators": 300,
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_child_samples": 500,
    "subsample": 0.7,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": 42,
    "n_jobs": 1,
    "verbose": -1,
    "deterministic": True,
    "force_row_wise": True,
}

TRIAL_CONTEXT = "discover_pooled_ml"


def _own_calendar(close: pd.DataFrame, fn: Any) -> pd.DataFrame:
    """Apply *fn* to each symbol's series on its own trading days.

    The panel mixes exchanges: on a US holiday the Euro Stoxx names trade
    and the S&P names are NaN. A full-window rolling statistic over the
    union index would be NaN around every foreign holiday, and a shift of
    n rows would not be n of the symbol's own sessions.
    """
    out = {sym: fn(close[sym].dropna()) for sym in close.columns}
    return pd.DataFrame(out).reindex(close.index)


def _feature_block(close: pd.Series) -> pd.DataFrame:
    ret = close.pct_change()
    vol_21 = ret.rolling(21, min_periods=21).std()
    vol_63 = ret.rolling(63, min_periods=63).std()
    return pd.DataFrame({
        "ret_5d": close / close.shift(5) - 1.0,
        "ret_21d": close / close.shift(21) - 1.0,
        "ret_63d": close / close.shift(63) - 1.0,
        "ret_126d": close / close.shift(126) - 1.0,
        "mom_12_1": close.shift(21) / close.shift(252) - 1.0,
        "vol_21d": vol_21,
        "vol_63d": vol_63,
        "vol_ratio": vol_21 / vol_63.replace(0.0, np.nan),
        "max_ret_21d": ret.rolling(21, min_periods=21).max(),
        "dist_52w_high": close / close.rolling(252, min_periods=252).max() - 1.0,
    })


def raw_features(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Each feature as a (date x symbol) frame, before ranking."""
    close = close.sort_index()
    blocks = {sym: _feature_block(cast(pd.Series, close[sym]).dropna()) for sym in close.columns}
    return {
        name: pd.DataFrame({sym: block[name] for sym, block in blocks.items()}).reindex(close.index)
        for name in FEATURES
    }


def forward_label(close: pd.DataFrame) -> pd.DataFrame:
    """labels.vol_scaled_forward_return per symbol on its own sessions."""
    from app.lab.quant_ml.labels import vol_scaled_forward_return

    return _own_calendar(
        close.sort_index(),
        lambda s: vol_scaled_forward_return(s.to_frame(), horizon=HORIZON, vol_window=VOL_WINDOW)
        .iloc[:, 0].astype(float),
    )


def centred_rank(frame: pd.DataFrame) -> pd.DataFrame:
    """Percentile rank within each date (row), centred on zero: [-0.5, 0.5]."""
    return frame.rank(axis=1, pct=True) - 0.5


def long_frame(close: pd.DataFrame, *, with_label: bool = True) -> pd.DataFrame:
    """Stack ranked features (and the ranked label) into one row per (date, symbol).

    Rows missing any feature are dropped, as are dates whose complete
    cross-section is smaller than MIN_CROSS_SECTION. The raw label is kept
    as ``label_raw`` for evaluation.
    """
    close = close.sort_index()
    feats = raw_features(close)
    # Rank only over names with every feature on that date, so a missing
    # feature never shifts the others' ranks.
    mask = np.logical_and.reduce([f.notna().to_numpy() for f in feats.values()])
    mask_df = pd.DataFrame(mask, index=close.index, columns=close.columns)
    columns: dict[str, pd.Series] = {}
    for name in FEATURES:
        columns[name] = cast(pd.Series, centred_rank(feats[name].where(mask_df)).stack())
    if with_label:
        label = forward_label(close).where(mask_df)
        columns["label_raw"] = cast(pd.Series, label.stack())
        columns["label"] = cast(pd.Series, centred_rank(label).stack())
    frame = pd.DataFrame(columns)
    frame.index.names = ["date", "symbol"]
    frame = frame.dropna()
    sizes = frame.groupby(level="date").size()
    keep = sizes.index[sizes >= MIN_CROSS_SECTION]
    return frame.loc[frame.index.get_level_values("date").isin(keep)]


def build_model(name: str) -> Any:
    """A fresh sklearn Pipeline for a pre-registered model."""
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline

    if name == "ridge":
        return Pipeline([("model", Ridge(alpha=RIDGE_ALPHA))])
    if name == "gbm":
        from lightgbm import LGBMRegressor

        return Pipeline([("model", LGBMRegressor(**GBM_PARAMS))])
    raise ValueError(f"unknown pooled model {name!r}")


def daily_rank_ic(frame: pd.DataFrame, score: pd.Series) -> pd.Series:
    """Spearman correlation of *score* with the raw label, per date."""
    joined = pd.DataFrame({"score": score, "label": frame["label_raw"]}).dropna()

    def _ic(group: pd.DataFrame) -> float:
        if len(group) < MIN_CROSS_SECTION:
            return float("nan")
        score_rank = cast(pd.Series, group["score"]).rank()
        label_rank = cast(pd.Series, group["label"]).rank()
        return float(score_rank.corr(label_rank))

    return cast(pd.Series, joined.groupby(level="date").apply(_ic)).dropna()


@dataclass
class ModelCard:
    name: str
    ic: float
    icir: float
    nw_t: float
    n_dates: int
    passed: bool = False


@dataclass
class PooledStudy:
    cards: dict[str, ModelCard]
    baseline: ModelCard
    n_rows: int
    n_symbols: int
    first_date: str
    last_date: str
    folds: list[dict[str, Any]] = field(default_factory=list)

    @property
    def served(self) -> ModelCard:
        return self.cards[SERVED_MODEL]

    def as_dict(self) -> dict[str, Any]:
        def card(c: ModelCard) -> dict[str, Any]:
            return {"ic": round(c.ic, 5), "icir": round(c.icir, 4), "nw_t": round(c.nw_t, 3),
                    "n_dates": c.n_dates, "passed": c.passed}

        return {
            "design": "pooled_cross_sectional",
            "served_model": SERVED_MODEL,
            "horizon_days": HORIZON,
            "features": list(FEATURES),
            "models": {k: card(v) for k, v in self.cards.items()},
            "baseline_mom_12_1": card(self.baseline),
            "n_rows": self.n_rows,
            "n_symbols": self.n_symbols,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "folds": self.folds,
            "gate": {"min_ic": MIN_IC, "min_nw_t": _hlz_threshold()},
        }


def _hlz_threshold() -> float:
    from app.foundation.quant_metrics import HLZ_T_THRESHOLD

    return float(HLZ_T_THRESHOLD)


def _card(name: str, ic_series: pd.Series) -> ModelCard:
    from app.foundation.quant_metrics import newey_west_t_stat

    ic = float(ic_series.mean()) if len(ic_series) else 0.0
    # ICIR on non-overlapping dates: the daily series overlaps HORIZON-fold.
    spaced = ic_series.iloc[::HORIZON]
    sd = float(spaced.std(ddof=1)) if len(spaced) > 2 else float("nan")
    icir = float(spaced.mean()) / sd if sd and np.isfinite(sd) and sd > 0 else 0.0
    nw_t = newey_west_t_stat(ic_series.to_numpy(), lags=HORIZON)
    card = ModelCard(name=name, ic=ic, icir=icir, nw_t=float(nw_t), n_dates=len(ic_series))
    card.passed = card.ic >= MIN_IC and card.nw_t >= _hlz_threshold()
    return card


def _day(ts: Any) -> str:
    return str(pd.Timestamp(ts))[:10]


def run_study(close: pd.DataFrame, *, n_folds: int = N_FOLDS) -> tuple[PooledStudy, pd.DataFrame]:
    """Purged walk-forward evaluation of every pre-registered model.

    Returns the study and the long frame it was run on (so the caller can
    fit the served model on it without rebuilding).
    """
    from app.lab.quant_lab.cv import purged_embargo_splits

    frame = long_frame(close)
    if frame.empty:
        raise ValueError("no complete cross-section to train on")
    dates = frame.index.get_level_values("date")
    unique_dates = pd.DatetimeIndex(sorted(dates.unique()))
    folds = purged_embargo_splits(len(unique_dates), n_folds=n_folds, purge_days=HORIZON)

    oos: dict[str, list[pd.Series]] = {m: [] for m in MODELS}
    fold_rows: list[dict[str, Any]] = []
    X_all = frame[list(FEATURES)]
    for fold in folds:
        train_dates = unique_dates[fold.train_positions]
        test_dates = unique_dates[fold.test_positions]
        # Labels look HORIZON rows ahead, so the purge must be on dates the
        # label of a training row can reach: the splitter already drops
        # HORIZON dates before each test block.
        train_mask = dates.isin(train_dates)
        test_mask = dates.isin(test_dates)
        if not train_mask.any() or not test_mask.any():
            continue
        fold_rows.append({
            "fold": fold.fold, "train_rows": int(train_mask.sum()), "test_rows": int(test_mask.sum()),
            "test_start": _day(test_dates[0]), "test_end": _day(test_dates[-1]),
        })
        for m in MODELS:
            model = build_model(m).fit(X_all[train_mask], frame.loc[train_mask, "label"])
            oos[m].append(pd.Series(model.predict(X_all[test_mask]), index=frame.index[test_mask]))

    test_index = pd.concat(oos[MODELS[0]]).index if oos[MODELS[0]] else frame.index[:0]
    cards = {m: _card(m, daily_rank_ic(frame, pd.concat(oos[m]))) for m in MODELS if oos[m]}
    baseline = _card("mom_12_1", daily_rank_ic(frame, frame.loc[test_index, "mom_12_1"]))
    symbols = frame.index.get_level_values("symbol")
    study = PooledStudy(
        cards=cards, baseline=baseline, n_rows=len(frame), n_symbols=int(symbols.nunique()),
        first_date=_day(unique_dates[0]), last_date=_day(unique_dates[-1]), folds=fold_rows,
    )
    return study, frame


def fit_served(frame: pd.DataFrame) -> Any:
    """The served model fitted on every labelled row."""
    return build_model(SERVED_MODEL).fit(frame[list(FEATURES)], frame["label"])


def latest_raw_features(close: pd.DataFrame) -> pd.DataFrame:
    """(symbol x feature) raw values on each symbol's own last date.

    Symbols without complete features (too little history) are dropped.
    The frame's ``as_of`` column holds that date.
    """
    feats = raw_features(close)
    rows: dict[str, dict[str, Any]] = {}
    for sym in close.columns:
        last = close[sym].last_valid_index()
        if last is None:
            continue
        values = {name: feats[name].at[last, sym] for name in FEATURES}
        if any(pd.isna(v) for v in values.values()):
            continue
        rows[sym] = {**values, "as_of": last}
    return pd.DataFrame.from_dict(rows, orient="index")


def score_cross_section(model: Any, raw: pd.DataFrame) -> pd.Series:
    """Rank *raw* features within the given cross-section, predict, and
    return each symbol's prediction as a percentile (0-1) of the cross-section."""
    ranked = raw[list(FEATURES)].astype(float).rank(pct=True) - 0.5
    pred = pd.Series(model.predict(ranked), index=raw.index)
    return pred.rank(pct=True)


# ---------------------------------------------------------------------------
# Persistence and serving
# ---------------------------------------------------------------------------

POOLED_MODEL_NAME = "discover-ml-pooled"
# Stored in QuantMlModel.feature_schema_version; bump with FEATURES or the
# ranking scheme so an old artefact is never served new columns.
POOLED_SCHEMA_VERSION = 1


def spec_hash() -> str:
    """Identity of the pre-registered spec: a change is a new trial."""
    import hashlib
    import json

    spec = {
        "features": FEATURES, "horizon": HORIZON, "vol_window": VOL_WINDOW,
        "min_cross_section": MIN_CROSS_SECTION, "ridge_alpha": RIDGE_ALPHA,
        "gbm": GBM_PARAMS, "served": SERVED_MODEL, "schema": POOLED_SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]


def train_pooled_model(db: Any, user_id: str, close: pd.DataFrame, *, model_dir: str | None = None) -> Any:
    """Run the study, record it, and persist the served model if it passes.

    Always writes a ``QuantMlModel`` row (``name=POOLED_MODEL_NAME``, no
    ticker) with the study as ``metrics_json``: ``status="completed"`` with
    an artefact when the served model clears the gate, else
    ``"rejected_below_gate"`` and no artefact. Each pre-registered model is
    one trial on the global ledger per spec (idempotent), so weekly refits
    of the same spec do not inflate ``n_trials``.
    """
    import json
    from datetime import UTC, datetime

    from app.foundation.models.entities import QuantMlModel
    from app.foundation.quant_metrics import record_trial
    from app.lab.quant_ml.registry import save_model

    study, frame = run_study(close)
    key = spec_hash()
    for name in MODELS:
        record_trial(db, TRIAL_CONTEXT, f"{name}:{key}", {"model": name, "spec_hash": key, "pre_registered": True})

    served = study.served
    row = QuantMlModel(
        user_id=user_id,
        name=POOLED_MODEL_NAME,
        kind="lgbm",
        ticker=None,
        params_json=json.dumps({"spec_hash": key, "gbm": GBM_PARAMS, "ridge_alpha": RIDGE_ALPHA}),
        metrics_json=json.dumps(study.as_dict()),
        status="training",
        feature_schema_version=POOLED_SCHEMA_VERSION,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    if served.passed:
        fitted = fit_served(frame)
        row.artefact_path = save_model(fitted, row.id, base=model_dir) if model_dir else save_model(fitted, row.id)
        row.status = "completed"
    else:
        row.status = "rejected_below_gate"
        row.error_message = (
            f"out-of-sample rank IC {served.ic:.4f} (min {MIN_IC}), Newey-West t {served.nw_t:.2f} "
            f"(min {_hlz_threshold():.1f}); momentum 12-1 alone: IC {study.baseline.ic:.4f}"
        )
    row.finished_at = datetime.now(UTC)
    db.commit()
    return row


def latest_pooled_row(db: Any) -> Any:
    """The newest pooled QuantMlModel row of the current schema, any status."""
    from app.foundation.models.entities import QuantMlModel

    return (
        db.query(QuantMlModel)
        .filter(
            QuantMlModel.name == POOLED_MODEL_NAME,
            QuantMlModel.feature_schema_version == POOLED_SCHEMA_VERSION,
        )
        .order_by(QuantMlModel.created_at.desc())
        .first()
    )
