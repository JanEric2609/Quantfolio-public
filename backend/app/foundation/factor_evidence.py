"""Read side of the evidence that may unlock money in the monthly plan.

Factor-premia cards are written by ``app.lab.factor_premia`` and unlock the
tilt; evidence-gate runs are written by ``app.lab.evidence_gate`` and unlock
the stock-picking satellite. Read by the monthly plan (decision) and the
Evidence API. Kept in foundation so readers do not import the lab.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import EvidenceGateRun, FactorEvidenceCard

# The region whose cards gate the monthly plan's tilt. The tilt would be held
# through a world factor ETF next to an MSCI World core, so the evidence has
# to come from the same 23 markets; other regions are shown, never gating.
TILT_EVIDENCE_REGION = "world"


def factor_evidence_regions(db: Session) -> list[str]:
    """Regions with at least one stored run, the gating region first."""
    regions = {r for (r,) in db.query(FactorEvidenceCard.region).distinct()}
    return sorted(regions, key=lambda r: (r != TILT_EVIDENCE_REGION, r))


def latest_factor_evidence_cards(db: Session, region: str | None = None) -> list[dict[str, Any]]:
    """Cards of the most recent study run (card JSON plus ``computed_at``).

    With *region*, the most recent run for that region; otherwise the most
    recent run of any region.
    """
    query = db.query(FactorEvidenceCard)
    if region is not None:
        query = query.filter(FactorEvidenceCard.region == region)
    row = (
        query.with_entities(FactorEvidenceCard.run_id)
        .order_by(FactorEvidenceCard.computed_at.desc())
        .first()
    )
    if row is None:
        return []
    rows = db.query(FactorEvidenceCard).filter(FactorEvidenceCard.run_id == row[0]).all()
    return [
        {**(r.card_json or {}), "computed_at": r.computed_at.isoformat() if r.computed_at else None}
        for r in rows
    ]


def latest_evidence_gate(db: Session, region: str = TILT_EVIDENCE_REGION) -> dict[str, Any] | None:
    """The most recent evidence-gate verdict for *region*, or None if never run."""
    row = (
        db.query(EvidenceGateRun)
        .filter(EvidenceGateRun.region == region)
        .order_by(EvidenceGateRun.computed_at.desc())
        .first()
    )
    if row is None:
        return None
    return {
        "region": row.region,
        "satellite_unlocked": bool(row.satellite_unlocked),
        "unlocked_by": list(row.unlocked_by or []),
        "reason": row.reason,
        "n_trials": row.n_trials,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }
