"""Evidence API: surfaces the global trial ledger the DSR gate deflates against.

ADR 0015 (Honest Measurement Rebuild), ruling #12: a hard statistical gate is
only trustworthy if the numbers behind it are inspectable, not just the
verdict. This router is read-only — every write to the trial ledger happens
inside the context that runs the search (alphacrafter, advisor, discover,
graduation), never from an API handler — and exists purely so any of those
contexts' claimed ``n_trials`` can be checked against the ledger it actually
came from.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import TrialLedgerEntry, User
from app.foundation.auth import current_user
from app.foundation.quant_metrics import (
    DEFAULT_N_TRIALS_FLOOR,
    dsr_hurdle_curve,
    effective_n_trials,
    resolve_n_trials,
)
from app.lab.factor_premia import latest_cards

router = APIRouter(prefix="/api/evidence", tags=["evidence"])

# Five years of daily returns: the sample the hurdle curve is drawn for.
HURDLE_OBSERVATIONS = 5 * 252


@router.get("/n-trials")
def n_trials_summary(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """The ledger-backed global trial count every DSR call site deflates against.

    ``resolved`` is what every gate actually uses (the raw count, at least 1);
    ``raw_count``/``by_context`` are the ledger contents that number is
    derived from, so a claimed trial count can be checked rather than trusted.
    ``effective`` counts independent search families (a lower bound on the
    number of independent tests) and ``hurdle_curve`` the annualised Sharpe
    a strategy with five years of daily returns must beat at each N.
    """
    raw_count = db.query(func.count(TrialLedgerEntry.id)).scalar() or 0
    context_rows = (
        db.query(TrialLedgerEntry.context, func.count(TrialLedgerEntry.id))
        .group_by(TrialLedgerEntry.context)
        .all()
    )
    by_context = {row[0]: row[1] for row in context_rows}
    resolved = resolve_n_trials(db)
    effective = effective_n_trials(db)
    grid = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, resolved, effective]
    return {
        "resolved": resolved,
        "floor": DEFAULT_N_TRIALS_FLOOR,
        "raw_count": int(raw_count),
        "effective": effective,
        "by_context": by_context,
        "hurdle_observations": HURDLE_OBSERVATIONS,
        "hurdle_curve": dsr_hurdle_curve(grid, HURDLE_OBSERVATIONS),
    }


@router.get("/trial-ledger")
def trial_ledger_entries(
    context: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    """Most recent trial-ledger rows, optionally filtered to one ``context``."""
    query = db.query(TrialLedgerEntry)
    if context:
        query = query.filter(TrialLedgerEntry.context == context)
    rows = query.order_by(TrialLedgerEntry.created_at.desc()).limit(max(1, min(limit, 500))).all()
    return [
        {
            "id": r.id,
            "context": r.context,
            "trial_key": r.trial_key,
            "metadata": r.metadata_json or {},
            "registered_at": r.registered_at.isoformat() if r.registered_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


class FactorCheck(BaseModel):
    name: str
    label: str
    value: float | None
    passed: bool


class FactorEvidenceCardResponse(BaseModel):
    """One pre-registered factor strategy's prior-informed test (report Phase 3).

    Returns are decimals per year (0.02 = 2 %). ``long_only_annual`` is the
    top third minus the market; ``net_expected_annual`` is that after the
    post-publication decay haircut, the extra cost of a factor ETF and German
    tax. ``book_worst_5y_lag`` is the worst five-year excess scaled to the
    tilt cap: how far behind the whole book could have fallen.
    """

    strategy: str
    label: str
    region: str
    citation: str
    etf_hint: str
    months: int
    start: str | None
    end: str | None
    long_short_annual: float | None
    long_short_t: float | None
    post_publication_months: int
    post_publication_annual: float | None
    long_only_annual: float | None
    expected_annual: float | None
    net_expected_annual: float | None
    tracking_error_annual: float | None
    worst_5y_excess: float | None
    book_worst_5y_lag: float | None
    tilt_cap_pct: float
    passed: bool
    checks: list[FactorCheck]
    yearly_long_only: dict[str, float]
    computed_at: str | None


@router.get("/factor-premia", response_model=list[FactorEvidenceCardResponse])
def factor_premia_cards(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[FactorEvidenceCardResponse]:
    """Evidence cards of each region's latest factor-premia run, the gating
    (world) region first; empty if never run."""
    return [FactorEvidenceCardResponse(**card) for card in latest_cards(db)]


class IngredientAttributionRow(BaseModel):
    signal: str
    n_runs: int
    mean_ic: float | None
    t_stat: float | None
    p_value: float | None
    p_holm: float | None


class IngredientAttributionResponse(BaseModel):
    """Per-ingredient rank IC on live shadow-ledger data (ADR 0019 §5).

    Information only: it feeds no score and changes no gate. Each run is one
    cross-section (ingredient scores from ``components_json`` vs resolved
    forward excess returns); the IC series runs over runs. Stocks in one
    cohort move together, so ``effective_n_stocks`` (mean-pairwise-correlation
    adjustment) is fewer than the ~200 names.
    """

    horizon_days: int
    n_runs: int
    n_stocks: int
    rho_bar: float | None
    effective_n_stocks: float | None
    ingredients: list[IngredientAttributionRow]


#: Forward window the live attribution scores (about one month).
INGREDIENT_ATTRIBUTION_HORIZON_DAYS = 21


def _ingredient_frames(
    db: Session, user_id: str, horizon_days: int,
) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    """Per-ingredient (run, symbol, score, excess) frames plus the excess matrix."""
    from app.foundation.models.entities import CandidateOutcome, DiscoverCandidateSnapshot

    rows = (
        db.query(DiscoverCandidateSnapshot, CandidateOutcome)
        .join(CandidateOutcome, CandidateOutcome.snapshot_id == DiscoverCandidateSnapshot.id)
        .filter(
            DiscoverCandidateSnapshot.user_id == user_id,
            DiscoverCandidateSnapshot.evaluable.is_(True),
            DiscoverCandidateSnapshot.instrument_group == "stock",
            DiscoverCandidateSnapshot.backfilled.is_(False),
            CandidateOutcome.horizon_days == horizon_days,
            CandidateOutcome.status == "resolved",
            CandidateOutcome.excess_eur.is_not(None),
        )
        .all()
    )
    import pandas as pd

    frames: dict[str, list[dict[str, Any]]] = {}
    excess: dict[str, dict[str, float]] = {}
    # Newey-West lags must follow time, so each run is keyed by when it was
    # issued (run_id only breaks ties), not by its UUID.
    issued: dict[str, Any] = {}
    for snap, _ in rows:
        prev = issued.get(snap.run_id)
        if prev is None or snap.issued_at < prev:
            issued[snap.run_id] = snap.issued_at
    for snap, outcome in rows:
        period = f"{issued[snap.run_id].isoformat()}|{snap.run_id}"
        inputs = (snap.components_json or {}).get("composite_inputs") or []
        if not isinstance(inputs, list):
            continue
        for entry in inputs:
            if not isinstance(entry, dict) or entry.get("score") is None:
                continue
            try:
                score = float(entry["score"])
            except (TypeError, ValueError):
                continue
            frames.setdefault(str(entry.get("signal")), []).append({
                "period": period,
                "entity": snap.symbol,
                "score": score,
                "forward": float(outcome.excess_eur),
            })
        excess.setdefault(period, {})[snap.symbol] = float(outcome.excess_eur)
    return (
        {signal: pd.DataFrame(rows) for signal, rows in frames.items() if rows},
        excess,
    )


def _mean_pairwise_correlation(excess: dict[str, dict[str, float]]) -> float | None:
    """Mean pairwise correlation of forward excess returns (stocks × runs)."""
    import numpy as np
    import pandas as pd

    matrix = pd.DataFrame(excess).T.sort_index()
    matrix = matrix.dropna(axis=1, how="all")
    if matrix.shape[0] < 3 or matrix.shape[1] < 2:
        return None
    corr = matrix.corr(min_periods=3).to_numpy()
    off_diag = corr[~np.eye(corr.shape[0], dtype=bool)]
    values = off_diag[~np.isnan(off_diag)]
    if values.size == 0:
        return None
    return float(max(0.0, min(0.95, values.mean())))


@router.get("/ingredient-attribution", response_model=IngredientAttributionResponse)
def ingredient_attribution(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> IngredientAttributionResponse:
    """Weekly per-ingredient table with Holm-corrected p-values (ADR 0019 §5)."""
    from app.foundation.quant_metrics import holm_bonferroni
    from app.lab.ranking_replay import rank_ic_series, summarise

    frames, excess = _ingredient_frames(db, user.id, INGREDIENT_ATTRIBUTION_HORIZON_DAYS)
    summaries = {signal: summarise(rank_ic_series(frame)) for signal, frame in frames.items()}
    ordered = sorted(summaries)
    raw_ps: list[float] = []
    for s in ordered:
        p = summaries[s]["p_value"]
        raw_ps.append(p if p is not None else 1.0)
    holm_ps = dict(zip(ordered, holm_bonferroni(raw_ps), strict=True))

    rho = _mean_pairwise_correlation(excess)
    run_sizes = [len(stocks) for stocks in excess.values()]
    effective: float | None = None
    if run_sizes and rho is not None:
        effective = float(
            sum(n / (1.0 + (n - 1) * rho) for n in run_sizes) / len(run_sizes)
        )

    return IngredientAttributionResponse(
        horizon_days=INGREDIENT_ATTRIBUTION_HORIZON_DAYS,
        n_runs=len(excess),
        n_stocks=len({s for stocks in excess.values() for s in stocks}),
        rho_bar=rho,
        effective_n_stocks=effective,
        ingredients=[
            IngredientAttributionRow(
                signal=signal,
                n_runs=int(summaries[signal]["n_periods"]),
                mean_ic=summaries[signal]["mean_ic"],
                t_stat=summaries[signal]["t_stat"],
                p_value=summaries[signal]["p_value"],
                p_holm=holm_ps[signal],
            )
            for signal in ordered
        ],
    )
