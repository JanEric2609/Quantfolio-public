"""The newest completed Discover run's shortlist, as the mandates read it.

Every mandate reader used to take ``status == "shortlisted"`` rows across all
runs, ordered by ``id`` — a random UUID — so "the latest shortlist" was ten
shortlisted names from any past run (2026-11-09 audit). Both mandates read it
through here so they always see the same, current candidates: the same
top :data:`MANDATE_SHORTLIST_SIZE` names in the pipeline's own order
(composite, then portfolio fit, then symbol). Discover may shortlist up to
15; cutting an alphabetical list at ten would hand both arms whichever
tickers sort first.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoverCandidate, DiscoverRun


#: Names both mandates see; A's prompt block and B's rules read the same cut.
MANDATE_SHORTLIST_SIZE = 10


def _rank_key(candidate: DiscoverCandidate) -> tuple[float, float, str]:
    """``pipeline.shortlist_order`` on a stored candidate: best first when negated."""
    try:
        scores = json.loads(candidate.scores_json or "{}")
    except (TypeError, ValueError):
        scores = {}
    if not isinstance(scores, dict):
        scores = {}
    fit = scores.get("portfolio_fit")
    fit_score = fit.get("fit_score") if isinstance(fit, dict) else None
    composite = scores.get("composite")
    return (
        -(float(composite) if isinstance(composite, (int, float)) else 0.0),
        -(float(fit_score) if isinstance(fit_score, (int, float)) else 0.0),
        candidate.symbol or "",
    )


def latest_shortlist(
    db: Session, user_id: str | None = None, *, limit: int | None = MANDATE_SHORTLIST_SIZE
) -> list[DiscoverCandidate]:
    """The best *limit* shortlisted candidates of the newest completed Discover run (of *user_id* when given)."""
    query = db.query(DiscoverRun).filter(DiscoverRun.status == "completed")
    if user_id is not None:
        query = query.filter(DiscoverRun.user_id == user_id)
    run = query.order_by(DiscoverRun.created_at.desc()).first()
    if run is None:
        return []
    rows = (
        db.query(DiscoverCandidate)
        .filter(DiscoverCandidate.run_id == run.id, DiscoverCandidate.status == "shortlisted")
        .all()
    )
    rows.sort(key=_rank_key)
    return rows[:limit] if limit is not None else rows


def shortlist_dicts(db: Session, user_id: str | None = None, *, limit: int | None = MANDATE_SHORTLIST_SIZE) -> list[dict[str, Any]]:
    return [
        {"symbol": r.symbol, "name": r.name, "isin": r.isin, "scores_json": r.scores_json}
        for r in latest_shortlist(db, user_id, limit=limit)
    ]
