"""The newest completed Discover run's shortlist, as the mandates read it.

Every mandate reader used to take ``status == "shortlisted"`` rows across all
runs, ordered by ``id`` — a random UUID — so "the latest shortlist" was ten
shortlisted names from any past run (2026-11-09 audit). Both mandates read it
through here so they always see the same, current candidates.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoverCandidate, DiscoverRun


def latest_shortlist(db: Session, user_id: str | None = None, *, limit: int | None = None) -> list[DiscoverCandidate]:
    """Shortlisted candidates of the newest completed Discover run (of *user_id* when given)."""
    query = db.query(DiscoverRun).filter(DiscoverRun.status == "completed")
    if user_id is not None:
        query = query.filter(DiscoverRun.user_id == user_id)
    run = query.order_by(DiscoverRun.created_at.desc()).first()
    if run is None:
        return []
    rows = (
        db.query(DiscoverCandidate)
        .filter(DiscoverCandidate.run_id == run.id, DiscoverCandidate.status == "shortlisted")
        .order_by(DiscoverCandidate.symbol)
        .all()
    )
    return rows[:limit] if limit is not None else rows


def shortlist_dicts(db: Session, user_id: str | None = None, *, limit: int | None = 10) -> list[dict[str, Any]]:
    return [
        {"symbol": r.symbol, "name": r.name, "isin": r.isin, "scores_json": r.scores_json}
        for r in latest_shortlist(db, user_id, limit=limit)
    ]
