"""Descriptive ranking metrics from the shadow ledger (ADR 0018 §5).

Shown on the trust page, never a verdict (the verdict on the ranking is F2 in
``daily_tests.py``). Every number comes from frozen ``discover_candidate_snapshot``
rows and their ``candidate_outcome`` forward returns:

* per-run Spearman rank IC between the composite and the 21-day EUR return of
  the evaluable **stocks** of runs with at least 30 of them; the mean IC with a
  Newey-West t (lag 5 runs: weekly runs, 21-day windows overlap), the ICIR and
  the share of runs with IC > 0;
* IC decay across 5/10/21/63 trading days;
* quintile mean excess returns, and the picked names against the rest of the
  evaluable stocks (run-paired);
* sector-neutral IC (ranks demeaned within sector);
* the gate check: for each ``reject_stage``, the run-paired mean 21-day excess
  of rejected names minus evaluable ones, with a run-block bootstrap interval;
* ETFs as their own group, never pooled with stocks;
* score tiers (ADR 0018 §6): the share of top-, middle- and bottom-third
  stocks of each run that beat the ETF over 21 days, averaged over runs, with
  a Beta(1, 1) posterior range on the number of **runs** (the effective
  labels), next to the base rate of all scored stocks.

Live snapshots and back-filled ones (``backfilled = true``, exploratory) are
reported separately and never mixed.
"""
from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy.orm import Session

from app.decision.verification.daily_ledger import MIN_RANKING_STOCKS
from app.foundation import forecast_verification as fv
from app.foundation.models.entities import CandidateOutcome, DiscoverCandidateSnapshot

HORIZONS = (5, 10, 21, 63)
MAIN_HORIZON = 21
#: Newey-West lag in runs for the mean IC (ADR 0018 §5).
IC_HAC_LAG = 5
#: Fewest names a group needs in a run for an IC (ETFs are a small group).
MIN_ETFS = 10
MIN_SECTOR_NAMES = 3
N_QUINTILES = 5
CI_LEVEL = 0.9
_BOOTSTRAP_DRAWS = 2000
_SEED = 20261007


def _frame(db: Session, user_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    snaps = (
        db.query(
            DiscoverCandidateSnapshot.id, DiscoverCandidateSnapshot.run_id, DiscoverCandidateSnapshot.issue_date,
            DiscoverCandidateSnapshot.symbol, DiscoverCandidateSnapshot.instrument_group,
            DiscoverCandidateSnapshot.sector, DiscoverCandidateSnapshot.evaluable,
            DiscoverCandidateSnapshot.reject_stage, DiscoverCandidateSnapshot.composite,
            DiscoverCandidateSnapshot.picked, DiscoverCandidateSnapshot.backfilled,
            DiscoverCandidateSnapshot.cohort_id,
        )
        .filter(DiscoverCandidateSnapshot.user_id == user_id)
        .all()
    )
    s = pd.DataFrame.from_records(
        [tuple(r) for r in snaps],
        columns=["snapshot_id", "run_id", "issue_date", "symbol", "group", "sector", "evaluable", "reject_stage",
                 "composite", "picked", "backfilled", "cohort_id"],
    )
    for col in ("evaluable", "picked", "backfilled"):
        s[col] = s[col].astype(bool)
    s["composite"] = s["composite"].astype(float)
    outs = (
        db.query(
            CandidateOutcome.snapshot_id, CandidateOutcome.horizon_days, CandidateOutcome.ret_eur,
            CandidateOutcome.excess_eur, CandidateOutcome.status,
        )
        .filter(CandidateOutcome.user_id == user_id)
        .all()
    )
    o = pd.DataFrame.from_records([tuple(r) for r in outs], columns=["snapshot_id", "horizon", "ret", "excess", "status"])
    o[["ret", "excess"]] = o[["ret", "excess"]].astype(float)
    return s, o


def _rows(df: pd.DataFrame, mask: Any) -> pd.DataFrame:
    """Boolean row filter, typed (pandas-stubs widens ``df[mask]`` to a union)."""
    return cast(pd.DataFrame, df.loc[mask])


def _spearman(df: pd.DataFrame, x: str, y: str) -> float | None:
    a, b = cast(pd.Series, df[x]), cast(pd.Series, df[y])
    if len(a) < 3 or a.nunique() < 2 or b.nunique() < 2:
        return None
    return float(a.rank().corr(b.rank()))


def _ic_summary(ics: list[float]) -> dict[str, Any]:
    n = len(ics)
    if n == 0:
        return {"n_runs": 0, "mean": None, "t_nw": None, "icir": None, "share_positive": None}
    arr = np.asarray(ics, dtype=float)
    se = np.sqrt(fv.hac_variance_of_mean(arr, IC_HAC_LAG)) if n >= 2 else 0.0
    sd = float(arr.std(ddof=1)) if n >= 2 else None
    return {
        "n_runs": n,
        "mean": float(arr.mean()),
        "t_nw": float(arr.mean() / se) if se > 0 else None,
        "icir": float(arr.mean() / sd) if sd else None,
        "share_positive": float(np.mean(arr > 0)),
    }


def _run_block_ci(values: list[float]) -> list[float] | None:
    """Percentile interval of the mean, resampling whole runs."""
    if len(values) < fv.MIN_CALLS_FOR_CI:
        return None
    rng = np.random.default_rng(_SEED)
    arr = np.asarray(values, dtype=float)
    draws = rng.integers(0, arr.size, size=(_BOOTSTRAP_DRAWS, arr.size))
    means = arr[draws].mean(axis=1)
    lo, hi = np.quantile(means, [(1 - CI_LEVEL) / 2, (1 + CI_LEVEL) / 2])
    return [float(lo), float(hi)]


TIERS = ("top", "middle", "bottom")


def _beta_range(rate: float, n_eff: int) -> list[float]:
    a, b = 1.0 + rate * n_eff, 1.0 + (1.0 - rate) * n_eff
    return [float(stats.beta.ppf((1 - CI_LEVEL) / 2, a, b)), float(stats.beta.ppf((1 + CI_LEVEL) / 2, a, b))]


def _tiers(main_stocks: pd.DataFrame) -> dict[str, Any]:
    """Hit rate (excess > 0 at 21 days) per score third, run-weighted, with the base rate."""
    per_tier: dict[str, list[float]] = {t: [] for t in TIERS}
    base: list[float] = []
    dates: set[Any] = set()
    for _run, df in main_stocks.groupby("run_id"):
        df = _rows(df, df["excess"].notna())
        if len(df) < MIN_RANKING_STOCKS:
            continue
        dates.add(df["issue_date"].iloc[0])
        third = np.ceil(df["composite"].rank(method="first", ascending=False) / len(df) * 3).clip(1, 3)
        hit = df["excess"] > 0
        base.append(float(hit.mean()))
        for k, tier in enumerate(TIERS, start=1):
            per_tier[tier].append(float(hit[third == k].mean()))
    n_eff = len(dates)

    def summary(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n_runs": 0, "hit_rate": None, "range": None}
        rate = float(np.mean(values))
        return {"n_runs": len(values), "hit_rate": rate, "range": _beta_range(rate, n_eff)}

    return {
        "n_eff": n_eff,
        "base": summary(base),
        "tiers": [{"tier": t, **summary(per_tier[t])} for t in TIERS],
    }


def _sector_neutral(df: pd.DataFrame) -> float | None:
    sized = _rows(df, df["sector"].notna())
    sector_size = cast(pd.Series, sized.groupby("sector")["symbol"].transform("size"))
    sized = _rows(sized, sector_size >= MIN_SECTOR_NAMES)
    if len(sized) < 3:
        return None
    rc = sized.groupby("sector")["composite"].rank()
    rr = sized.groupby("sector")["ret"].rank()
    a = rc - rc.groupby(sized["sector"]).transform("mean")
    b = rr - rr.groupby(sized["sector"]).transform("mean")
    if a.std() == 0 or b.std() == 0:
        return None
    return float(a.corr(b))


def _section(snaps: pd.DataFrame, outs: pd.DataFrame) -> dict[str, Any]:
    """Every metric for one set of snapshots (live or back-filled)."""
    stocks = _rows(snaps, (snaps["group"] == "stock") & snaps["evaluable"] & snaps["composite"].notna())
    n_eval = cast(pd.Series, stocks.groupby("run_id")["symbol"].size())
    cohort_runs = {run for run, n in n_eval.items() if n >= MIN_RANKING_STOCKS}
    merged = snaps.merge(outs, on="snapshot_id", how="inner")
    priced = _rows(merged, merged["ret"].notna())

    decay: list[dict[str, Any]] = []
    main_ics: list[float] = []
    sector_ics: list[float] = []
    for h in HORIZONS:
        ics: list[float] = []
        at_h = _rows(priced, (priced["horizon"] == h) & priced["run_id"].isin(list(cohort_runs)))
        at_h = _rows(at_h, (at_h["group"] == "stock") & at_h["evaluable"] & at_h["composite"].notna())
        for _run, df in at_h.groupby("run_id"):
            if len(df) < MIN_RANKING_STOCKS:
                continue
            ic = _spearman(df, "composite", "ret")
            if ic is not None:
                ics.append(ic)
            if h == MAIN_HORIZON:
                sn = _sector_neutral(df)
                if sn is not None:
                    sector_ics.append(sn)
        if h == MAIN_HORIZON:
            main_ics = ics
        summary = _ic_summary(ics)
        decay.append({"horizon_days": h, "n_runs": summary["n_runs"], "mean_ic": summary["mean"]})

    main = _rows(priced, (priced["horizon"] == MAIN_HORIZON) & priced["run_id"].isin(list(cohort_runs)))
    main_stocks = _rows(main, (main["group"] == "stock") & main["evaluable"] & main["composite"].notna())

    quintiles: list[list[float]] = [[] for _ in range(N_QUINTILES)]
    picked_gap: list[float] = []
    for _run, df in main_stocks.groupby("run_id"):
        df = _rows(df, df["excess"].notna())
        if len(df) < MIN_RANKING_STOCKS:
            continue
        # Quintile 1 = the highest composites.
        q = pd.qcut(df["composite"].rank(method="first", ascending=False), N_QUINTILES, labels=False)
        for k, mean in df.groupby(q)["excess"].mean().items():
            quintiles[int(cast(int, k))].append(float(mean))
        top, rest = _rows(df, df["picked"]), _rows(df, ~df["picked"])
        if len(top) and len(rest):
            picked_gap.append(float(top["excess"].mean() - rest["excess"].mean()))

    tiers = _tiers(main_stocks)

    gate: list[dict[str, Any]] = []
    main_all = _rows(merged, (merged["horizon"] == MAIN_HORIZON) & merged["excess"].notna())
    rejected = _rows(main_all, ~main_all["evaluable"] & main_all["reject_stage"].notna())
    evaluable = _rows(main_all, main_all["evaluable"] & (main_all["group"] == "stock"))
    eval_mean = cast(pd.Series, evaluable.groupby("run_id")["excess"].mean())
    for stage, df in rejected.groupby("reject_stage"):
        per_run = cast(pd.Series, df.groupby("run_id")["excess"].mean())
        paired = (per_run - eval_mean.reindex(per_run.index)).dropna()
        values = [float(v) for v in paired]
        gate.append({
            "reject_stage": str(stage),
            "n_runs": len(values),
            "n_names": int(len(df)),
            "mean_gap": float(np.mean(values)) if values else None,
            "ci": _run_block_ci(values),
        })
    gate.sort(key=lambda g: (-g["n_names"], g["reject_stage"]))

    etf_ics: list[float] = []
    etfs = _rows(priced, (priced["horizon"] == MAIN_HORIZON) & (priced["group"] == "etf") & priced["evaluable"])
    for _run, df in _rows(etfs, etfs["composite"].notna()).groupby("run_id"):
        if len(df) >= MIN_ETFS and (ic := _spearman(df, "composite", "ret")) is not None:
            etf_ics.append(ic)

    statuses = merged.groupby("status").size().to_dict() if len(merged) else {}
    return {
        "n_runs": int(snaps["run_id"].nunique()),
        "n_cohort_runs": len(cohort_runs),
        "n_snapshots": int(len(snaps)),
        "n_outcomes": int(len(merged)),
        "outcome_status": {str(k): int(v) for k, v in statuses.items()},
        "first_issue": snaps["issue_date"].min().isoformat() if len(snaps) else None,
        "last_issue": snaps["issue_date"].max().isoformat() if len(snaps) else None,
        "ic": _ic_summary(main_ics),
        "ic_decay": decay,
        "sector_neutral_ic": _ic_summary(sector_ics),
        "quintiles": [
            {"quintile": k + 1, "n_runs": len(v), "mean_excess": float(np.mean(v)) if v else None}
            for k, v in enumerate(quintiles)
        ],
        "picked_vs_rest": {
            "n_runs": len(picked_gap),
            "mean_gap": float(np.mean(picked_gap)) if picked_gap else None,
            "ci": _run_block_ci(picked_gap),
        },
        "gate_check": gate,
        "tiers": tiers,
        "etf_ic": _ic_summary(etf_ics),
    }


def build_ranking_metrics(db: Session, user_id: str) -> dict[str, Any]:
    """Live and exploratory (back-filled) ranking metrics for one user."""
    snaps, outs = _frame(db, user_id)
    live = _rows(snaps, ~snaps["backfilled"])
    backfilled = _rows(snaps, snaps["backfilled"])
    return {
        "horizon_days": MAIN_HORIZON,
        "min_stocks": MIN_RANKING_STOCKS,
        "ic_hac_lag": IC_HAC_LAG,
        "live": _section(live, outs),
        "exploratory": _section(backfilled, outs) if len(backfilled) else None,
        "verdict_note": (
            "Descriptive only. Whether the ranking works is decided by the pre-registered daily test (F2), "
            "not by these numbers."
        ),
    }


__all__ = ["build_ranking_metrics"]
