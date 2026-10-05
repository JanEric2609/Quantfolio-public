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
