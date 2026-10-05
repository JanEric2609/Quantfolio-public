"""Run the pooled model under walk-forward CV and grade its out-of-sample record.

Two passes over the model panel. The first collects each month's least-squares
moments and the boosted trees' row sample. Then every fold is fitted on its
purged, embargoed training months only. The second pass scores each test month
with its own fold's models and the value + momentum baseline, and turns the
scores into the same portfolios ``factor_premia`` builds: terciles within
country and month, value-weighted legs, countries combined by market cap.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.orm import Session

from app.foundation.data_engineering.paths import get_panel_dir
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.foundation.models.entities._core import now_utc
from app.foundation.quant_metrics import newey_west_t_stat, record_trial
from app.lab.factor_premia.strategies import REGIONS
from app.lab.pooled_model.fit import GbmFit, Moments, RidgeFit, Sample, fit_gbm, fit_ridge
from app.lab.pooled_model.panel import (
    DERIVED_DIR,
    Chunk,
    iter_chunks,
    model_panel_path,
    panel_months,
    panel_rows,
)
from app.lab.pooled_model.spec import (
    BASELINE,
    BASELINE_FEATURES,
    EMBARGO_MONTHS,
    FEATURES,
    MODELS,
    N_FOLDS,
    NW_LAGS,
    PURGE_MONTHS,
    RECENT_YEARS,
)
from app.lab.quant_lab.cv import purged_embargo_splits

logger = logging.getLogger(__name__)

TRIAL_CONTEXT = "pooled_model"
SERIES_COLUMNS = ["model", "month", "long_short", "long_only", "ic", "n_stocks"]
# Per-stock out-of-sample scores, for the satellite study (step 3).
SCORE_COLUMNS = ["eom", "gvkey", "excntry", "size_grp", "me", "r", *MODELS, BASELINE]


@dataclass(frozen=True)
class FoldFit:
    fold: int
    train_months: int
    test_first: str
    test_last: str
    ridge: RidgeFit
    gbm: GbmFit | None

    def summary(self) -> dict[str, Any]:
        return {
            "fold": self.fold, "train_months": self.train_months,
            "test_first": self.test_first, "test_last": self.test_last,
            "ridge_lambda": self.ridge.lam, "gbm_trees": self.gbm.trees if self.gbm else None,
        }


@dataclass
class ModelCard:
    """The out-of-sample record of one model (or the baseline) in one region."""

    model: str
    region: str
    months: int
    first_month: str | None
    last_month: str | None
    ic_mean: float | None
    ic_t: float | None
    long_short_annual: float | None
    long_short_t: float | None
    long_short_sharpe: float | None
    long_only_annual: float | None
    vs_baseline_annual: float | None
    vs_baseline_t: float | None
    recent: dict[str, float | int | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelStudy:
    region: str
    cards: list[ModelCard]
    series: pd.DataFrame
    folds: list[dict[str, Any]]
    artifact: Path | None = None


# --- scoring --------------------------------------------------------------------


def _baseline_score(x: np.ndarray) -> np.ndarray:
    cols = [FEATURES.index(f) for f in BASELINE_FEATURES]
    return x[:, cols].astype(np.float64).mean(axis=1)


def month_series(frame: pd.DataFrame, score: str) -> pd.DataFrame:
    """Monthly tercile portfolios and rank IC for one score column.

    ``frame`` has one row per stock and month with ``eom``, ``excntry``,
    ``me``, ``r``, ``y`` (the centred return rank) and the score. Terciles use
    ``percent_rank`` (min rank for ties), as ``factor_premia`` does. The IC is
    the within-country Spearman correlation, averaged over countries by count.
    """
    keys = ["eom", "excntry"]
    grouped = frame.groupby(keys, sort=False)[score]
    n = grouped.transform("size").to_numpy(dtype=float)
    pct = (grouped.rank(method="min").to_numpy() - 1) / np.maximum(n - 1, 1)
    srank = grouped.rank(method="average").to_numpy() / n - 0.5
    top, bottom = pct >= 2.0 / 3, pct < 1.0 / 3
    me, r, y = frame["me"].to_numpy(), frame["r"].to_numpy(), frame["y"].to_numpy()
    parts = pd.DataFrame({
        "eom": frame["eom"].to_numpy(), "excntry": frame["excntry"].to_numpy(),
        "w_top": me * top, "wr_top": me * r * top, "w_bot": me * bottom, "wr_bot": me * r * bottom,
        "wr": me * r, "me": me, "n": 1.0,
        "sx": srank, "sy": y, "sxx": srank * srank, "syy": y * y, "sxy": srank * y,
    })
    c = cast(pd.DataFrame, parts.groupby(keys, sort=True).sum())
    count = c["n"].to_numpy()
    cov = c["sxy"].to_numpy() - c["sx"].to_numpy() * c["sy"].to_numpy() / count
    var = (c["sxx"].to_numpy() - c["sx"].to_numpy() ** 2 / count) * (
        c["syy"].to_numpy() - c["sy"].to_numpy() ** 2 / count
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        ic = np.where(var > 0, cov / np.sqrt(np.where(var > 0, var, 1.0)), np.nan)
        cap = c["me"].to_numpy()
        market = c["wr"].to_numpy() / cap
        # A score that cannot split a country-month (all tied) takes no position
        # there: both legs are the market, so the month counts as zero, not missing.
        split = (c["w_top"].to_numpy() > 0) & (c["w_bot"].to_numpy() > 0)
        top = np.where(split, c["wr_top"].to_numpy() / c["w_top"].to_numpy(), market)
        bottom = np.where(split, c["wr_bot"].to_numpy() / c["w_bot"].to_numpy(), market)
    has_ic = ~np.isnan(ic)
    # Countries combine by market cap; the IC by stock count.
    weighted = pd.DataFrame({
        "eom": c.index.get_level_values("eom"),
        "cap": cap, "top": top * cap, "bottom": bottom * cap, "market": market * cap,
        "ic": np.where(has_ic, ic * count, 0.0), "ic_n": np.where(has_ic, count, 0.0), "n": count,
    })
    m = cast(pd.DataFrame, weighted.groupby("eom", sort=True).sum())
    out = pd.DataFrame({
        "month": pd.PeriodIndex(pd.DatetimeIndex(m.index), freq="M") + 1,
        "long_short": ((m["top"] - m["bottom"]) / m["cap"]).to_numpy(),
        "long_only": ((m["top"] - m["market"]) / m["cap"]).to_numpy(),
        "ic": (m["ic"] / m["ic_n"].where(m["ic_n"] > 0)).to_numpy(),
        "n_stocks": m["n"].to_numpy().astype(int),
    })
    return out


def _scores(chunk: Chunk, rows: np.ndarray, fit: FoldFit) -> dict[str, np.ndarray]:
    x = chunk.x[rows]
    scores = {"ridge": fit.ridge.predict(x), BASELINE: _baseline_score(x)}
    if fit.gbm is not None:
        scores["gbm"] = fit.gbm.predict(x)
    return scores


# --- grading --------------------------------------------------------------------


def _annual(values: np.ndarray) -> float | None:
    return float(values.mean() * 12) if len(values) else None


def _t(values: np.ndarray) -> float | None:
    return float(newey_west_t_stat(values, NW_LAGS)) if len(values) > NW_LAGS + 1 else None


def _sharpe(values: np.ndarray) -> float | None:
    if len(values) < 2 or values.std(ddof=1) == 0:
        return None
    return float(values.mean() / values.std(ddof=1) * np.sqrt(12))


def grade(model: str, region: str, series: pd.DataFrame, baseline: pd.DataFrame | None) -> ModelCard:
    s = series.sort_values("month")
    ls, lo, ic = s["long_short"].to_numpy(), s["long_only"].to_numpy(), s["ic"].dropna().to_numpy()
    diff = np.array([])
    if baseline is not None and model != BASELINE:
        joined = s.merge(baseline[["month", "long_short"]], on="month", suffixes=("", "_base"))
        diff = (joined["long_short"] - joined["long_short_base"]).to_numpy()
    recent = s.tail(RECENT_YEARS * 12)
    rls = recent["long_short"].to_numpy()
    return ModelCard(
        model=model, region=region, months=len(s),
        first_month=str(s["month"].iloc[0]) if len(s) else None,
        last_month=str(s["month"].iloc[-1]) if len(s) else None,
        ic_mean=float(ic.mean()) if len(ic) else None, ic_t=_t(ic),
        long_short_annual=_annual(ls), long_short_t=_t(ls), long_short_sharpe=_sharpe(ls),
        long_only_annual=_annual(lo),
        vs_baseline_annual=_annual(diff), vs_baseline_t=_t(diff),
        recent={
            "months": len(recent),
            "ic_mean": float(recent["ic"].mean()) if len(recent) else None,
            "long_short_annual": _annual(rls), "long_short_t": _t(rls),
        },
    )


# --- the study ------------------------------------------------------------------


def _fit_folds(path: Path, months: list[pd.Timestamp], n_folds: int) -> list[FoldFit]:
    moments = Moments.empty(len(months), len(FEATURES))
    sample = Sample.for_rows(panel_rows(path))
    for chunk in iter_chunks(path, months):
        moments.add(chunk)
        sample.add(chunk)
    rows = sample.arrays()
    folds = purged_embargo_splits(
        len(months), n_folds=n_folds, purge_days=PURGE_MONTHS, embargo_frac=EMBARGO_MONTHS / len(months),
    )
    fits = []
    for fold in folds:
        train = fold.train_positions
        fits.append(FoldFit(
            fold=fold.fold, train_months=len(train),
            test_first=str(months[fold.test_positions[0]].date()),
            test_last=str(months[fold.test_positions[-1]].date()),
            ridge=fit_ridge(moments, train), gbm=fit_gbm(rows, train),
        ))
        logger.info("pooled_model: fold %d fitted (%d training months)", fold.fold, len(train))
    return fits


def scores_path(panel_dir: Path, region: str) -> Path:
    """Where ``run_model_study`` writes the per-stock out-of-sample scores."""
    return panel_dir / DERIVED_DIR / f"pooled_model_{region}_scores.parquet"


def _nullable(values: np.ndarray) -> pa.Array:
    """float32 with NaN stored as null, so DuckDB's ordering and quantiles skip it."""
    values = values.astype(np.float32)
    return pa.array(values, type=pa.float32(), mask=np.isnan(values))


def _score_table(chunk: Chunk, rows: np.ndarray, scores: dict[str, np.ndarray]) -> pa.Table:
    n = int(rows.sum())
    missing = np.full(n, np.nan, dtype=np.float32)
    return pa.table({
        "eom": pa.array(chunk.eom[rows].astype("datetime64[D]"), type=pa.date32()),
        "gvkey": pa.array(chunk.gvkey[rows].astype(str), type=pa.string()),
        "excntry": pa.array(chunk.excntry[rows].astype(str), type=pa.string()),
        "size_grp": pa.array([None if v is None else str(v) for v in chunk.size_grp[rows]], type=pa.string()),
        "me": pa.array(chunk.me[rows], type=pa.float64()),
        "r": pa.array(chunk.r[rows], type=pa.float64()),
        **{name: _nullable(scores.get(name, missing)) for name in (*MODELS, BASELINE)},
    })


def _test_series(
    path: Path, months: list[pd.Timestamp], fits: list[FoldFit], n_folds: int, scores_out: Path | None = None,
) -> pd.DataFrame:
    folds = purged_embargo_splits(
        len(months), n_folds=n_folds, purge_days=PURGE_MONTHS, embargo_frac=EMBARGO_MONTHS / len(months),
    )
    fold_of = np.full(len(months), -1)
    for fold in folds:
        fold_of[fold.test_positions] = fold.fold
    by_fold = {f.fold: f for f in fits}
    parts: list[pd.DataFrame] = []
    writer: pq.ParquetWriter | None = None
    tmp = scores_out.with_suffix(".parquet.tmp") if scores_out else None
    try:
        for chunk in iter_chunks(path, months):
            row_fold = fold_of[chunk.month]
            for k in np.unique(row_fold[row_fold >= 0]):
                rows = row_fold == k
                frame = pd.DataFrame({
                    "eom": chunk.eom[rows], "excntry": chunk.excntry[rows],
                    "me": chunk.me[rows], "r": chunk.r[rows], "y": chunk.y[rows],
                })
                scores = _scores(chunk, rows, by_fold[int(k)])
                for name, score in scores.items():
                    frame[name] = score
                    monthly = month_series(frame, name)
                    monthly.insert(0, "model", name)
                    parts.append(monthly)
                if tmp is not None:
                    table = _score_table(chunk, rows, scores)
                    writer = writer or pq.ParquetWriter(tmp, table.schema)
                    writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    if tmp is not None and scores_out is not None and writer is not None:
        tmp.replace(scores_out)
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame(columns=pd.Index(SERIES_COLUMNS))
    return pd.concat(parts, ignore_index=True).sort_values(["model", "month"]).reset_index(drop=True)


def results_path(panel_dir: Path, region: str) -> Path:
    """Where ``run_model_study`` writes the cards and monthly series."""
    return panel_dir / DERIVED_DIR / f"pooled_model_{region}_results.json"


def _write_artifact(panel_dir: Path, study: ModelStudy) -> Path:
    out = results_path(panel_dir, study.region)
    out.parent.mkdir(parents=True, exist_ok=True)
    series = study.series.assign(month=study.series["month"].astype(str))
    out.write_text(json.dumps({
        "computed_at": now_utc().isoformat(), "region": study.region,
        "cards": [c.as_dict() for c in study.cards], "folds": study.folds,
        "series": series.to_dict(orient="records"),
    }, indent=1, default=float))
    return out


def run_model_study(
    db: Session,
    *,
    region: str = TILT_EVIDENCE_REGION,
    panel_dir: Path | None = None,
    persist: bool = True,
    n_folds: int = N_FOLDS,
) -> ModelStudy | None:
    """Fit, score and grade the pre-registered models; None without data.

    With ``persist`` each model is recorded once on the global trial ledger
    and the cards, fold summaries and monthly series are written as JSON next
    to the panel (``derived/pooled_model_<region>_results.json``) for the
    cost (step 3) and DSR/PBO (step 4) stages. Each stock's out-of-sample
    scores go to ``derived/pooled_model_<region>_scores.parquet``
    (``scores_path``), which the satellite study (step 3) picks from.
    """
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; expected one of {sorted(REGIONS)}")
    panel_dir = panel_dir or get_panel_dir()
    path = model_panel_path(panel_dir, region)
    months = panel_months(path) if path else []
    if path is None or not months:
        logger.warning("pooled_model: no JKP rows for region %s", region)
        return None

    fits = _fit_folds(path, months, n_folds)
    scores_out = None
    if persist:
        scores_out = scores_path(panel_dir, region)
        scores_out.parent.mkdir(parents=True, exist_ok=True)
    series = _test_series(path, months, fits, n_folds, scores_out)
    def _of(name: str) -> pd.DataFrame:
        return cast(pd.DataFrame, series[series["model"] == name])

    baseline = _of(BASELINE)
    cards = [grade(name, region, _of(name), baseline if len(baseline) else None) for name in (*MODELS, BASELINE)]
    study = ModelStudy(region, cards, series, [f.summary() for f in fits])
    if persist:
        for name in MODELS:
            record_trial(db, TRIAL_CONTEXT, f"{region}:{name}", {"region": region, "model": name, "pre_registered": True})
        db.commit()
        study.artifact = _write_artifact(panel_dir, study)
        logger.info("pooled_model: wrote %s", study.artifact)
    return study
