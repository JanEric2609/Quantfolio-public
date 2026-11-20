"""Dated copies of the cached fundamentals (plan 2026-11-11, ML spec v2).

``fundamentals`` holds the latest values per ticker and is overwritten on
every refresh, so it has no history. ``snapshot_fundamentals`` copies each
row once, under the UTC day it was fetched. A row is never updated after it
is written, and an unchanged cache row is not copied again.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.foundation.models.entities import Fundamental, FundamentalsSnapshot

#: A cache row older than this was not refreshed by the recent runs; copying
#: it would date old values to a day they were not fetched.
MAX_AGE = timedelta(days=3)


def snapshot_fundamentals(db: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Copy fresh, non-empty cache rows into dated snapshots."""
    now = now or datetime.now(UTC)
    rows = db.query(Fundamental).filter(Fundamental.fetched_at >= now - MAX_AGE).all()
    stored = skipped = 0
    for row in rows:
        fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
        day = fetched.astimezone(UTC).date()
        try:
            payload = json.loads(row.data_json or "{}")
        except ValueError:
            payload = {}
        if not payload:
            skipped += 1
            continue
        exists = (
            db.query(FundamentalsSnapshot.id)
            .filter(FundamentalsSnapshot.symbol == row.ticker, FundamentalsSnapshot.snapshot_date == day)
            .first()
        )
        if exists is not None:
            continue
        db.add(FundamentalsSnapshot(
            symbol=row.ticker, snapshot_date=day, data_json=row.data_json, source=row.source or "",
        ))
        stored += 1
    db.commit()
    return {"stored": stored, "skipped_empty": skipped}
