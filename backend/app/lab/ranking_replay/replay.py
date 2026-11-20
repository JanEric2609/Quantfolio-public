"""Historical replay of Discover's non-AI ingredients (ADR 0019 §4, lab only).

Input: per-ingredient cross-sections — one row per (period, stock) with the
ingredient's score and the forward excess return over the same window.
Output: rank IC per period, its Newey-West t (overlapping periods allowed),
Holm-corrected p-values across ingredients, a written yes/no, and one trial
ledger row per test.

The locked exam is calendar years 2019–2025, opened once, at the end: frames
outside that window are refused, and re-running a spec never adds trials.
"""
from __future__ import annotations

import math
from typing import Any, TypedDict, cast

import pandas as pd
from scipy.stats import norm as _norm
from scipy.stats import spearmanr
from sqlalchemy.orm import Session

from app.foundation.quant_metrics import holm_bonferroni, newey_west_t_stat, record_trial

#: Discover's composite ingredients that don't use AI (ADR 0019 §4).
NON_AI_INGREDIENTS: tuple[str, ...] = ("momentum", "risk", "benchmark", "portfolio", "fundamentals")

#: AI- or external-data-dependent signals: refused as replay inputs.
AI_INGREDIENTS: tuple[str, ...] = (
    "ic_icir", "analyst", "sentiment", "ml_signal", "estimate_revision", "insider_signal",
)

#: The locked final exam, opened once, at the end.
EXAM_START_YEAR = 2019
EXAM_END_YEAR = 2025

TRIAL_CONTEXT = "discover_replay"
NEWEY_WEST_LAGS = 6
#: Prior-informed bar, like the factor tilt — not t > 3, this is a
#: pre-registered confirmation, not a mined discovery.
PASS_T = 2.0


def rank_ic_series(frame: pd.DataFrame) -> pd.Series:
    """Per-period Spearman rank IC between ``score`` and ``forward``.

    Periods with fewer than 3 joint observations carry no signal and are
    skipped. Ties share ranks (Spearman); a constant score ranks nothing and
    yields NaN for that period.
    """
    out: dict[Any, float] = {}
    grouped = cast(Any, frame.groupby("period", sort=True))
    for period, group in grouped:
        pairs = cast(pd.DataFrame, group[["score", "forward"]]).dropna()
        if len(pairs) < 3:
            continue
        if float(pairs["score"].nunique()) < 2 or float(pairs["forward"].nunique()) < 2:
            continue
        result = cast(Any, spearmanr(pairs["score"], pairs["forward"]))
        out[period] = float(result.statistic)
    return pd.Series(out, dtype=float)


def _t_to_p(t_stat: float) -> float:
    return float(2.0 * _norm.sf(abs(t_stat)))


class IngredientSummary(TypedDict):
    n_periods: int
    mean_ic: float | None
    t_stat: float | None
    p_value: float | None


def summarise(ic: pd.Series) -> IngredientSummary:
    """Mean IC and its Newey-West t over the IC series (NaN periods dropped)."""
    clean = cast(pd.Series, pd.to_numeric(ic, errors="coerce")).dropna()
    n = int(clean.size)
    if n < 3:
        return {"n_periods": n, "mean_ic": None, "t_stat": None, "p_value": None}
    t_stat = float(newey_west_t_stat(clean.to_numpy(), lags=NEWEY_WEST_LAGS))
    if not math.isfinite(t_stat):
        return {"n_periods": n, "mean_ic": float(clean.mean()), "t_stat": None, "p_value": None}
    return {
        "n_periods": n,
        "mean_ic": float(clean.mean()),
        "t_stat": t_stat,
        "p_value": _t_to_p(t_stat),
    }


def _check_years(frame: pd.DataFrame, years: tuple[int, int]) -> None:
    start, end = years
    if start != EXAM_START_YEAR or end != EXAM_END_YEAR:
        raise ValueError(
            f"the locked exam is {EXAM_START_YEAR}–{EXAM_END_YEAR}, opened once (got {start}–{end})"
        )
    periods = pd.to_datetime(frame["period"], errors="coerce")
    if periods.isna().any():
        raise ValueError("replay periods must be parseable dates")
    if int(periods.dt.year.min()) < start or int(periods.dt.year.max()) > end:
        raise ValueError("replay inputs must lie inside the locked 2019–2025 exam window")


def _check_ingredients(frames: dict[str, pd.DataFrame]) -> None:
    unknown = [k for k in frames if k not in (*NON_AI_INGREDIENTS, "composite")]
    if unknown:
        raise ValueError(f"not replayable ingredients: {sorted(unknown)}")
    ai = [k for k in frames if k in AI_INGREDIENTS]
    if ai:  # unreachable given the line above, kept as the explicit AI guard
        raise ValueError(f"AI ingredients are excluded from the replay: {sorted(ai)}")


def run_replay(
    db: Session,
    frames: dict[str, pd.DataFrame],
    *,
    years: tuple[int, int] = (EXAM_START_YEAR, EXAM_END_YEAR),
    spec_version: int = 1,
    persist: bool = True,
) -> dict[str, Any]:
    """Replay every ingredient and the composite; return the written verdict.

    With ``persist`` each ingredient (and the composite) is recorded once on
    the global trial ledger — idempotent per spec, so re-inspecting the exam
    never widens the search. Verdict "yes" iff the composite's Newey-West t
    clears ``PASS_T``; per-ingredient Holm-adjusted p-values accompany it so
    no single voice can be cherry-picked.
    """
    _check_ingredients(frames)
    for frame in frames.values():
        _check_years(frame, years)

    summaries = {name: summarise(rank_ic_series(frame)) for name, frame in frames.items()}
    ingredients = [k for k in summaries if k in NON_AI_INGREDIENTS]
    raw_ps = [s["p_value"] if s["p_value"] is not None else 1.0 for s in
              (summaries[k] for k in ingredients)]
    adjusted = holm_bonferroni(raw_ps)
    holm_ps = dict(zip(ingredients, adjusted, strict=True))

    composite_t = summaries.get("composite", {}).get("t_stat")
    verdict = "yes" if composite_t is not None and composite_t >= PASS_T else "no"

    if persist:
        for name, summary in summaries.items():
            record_trial(
                db, TRIAL_CONTEXT, f"exam-v{spec_version}:{name}",
                {
                    "family": f"exam-v{spec_version}",
                    "ingredient": name,
                    "years": list(years),
                    "n_periods": summary["n_periods"],
                    "mean_ic": summary["mean_ic"],
                    "t_stat": summary["t_stat"],
                    "p_holm": holm_ps.get(name),
                    "verdict": verdict,
                },
            )
        db.commit()

    return {
        "years": list(years),
        "spec_version": spec_version,
        "ingredients": {
            name: {**summary, "p_holm": holm_ps.get(name)} for name, summary in summaries.items()
        },
        "pass_t": PASS_T,
        "verdict": verdict,
    }
