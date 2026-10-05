"""Concurrency-safe upsert for the shared ``metrics_snapshots`` table.

``MetricsSnapshot`` carries a UNIQUE constraint on
``(portfolio_id, as_of, context)``, so the obvious select-then-insert is a race:
the loser raises ``IntegrityError`` on flush, and because both write paths
commit the snapshot row *in the same transaction* as their legacy column write
(``PaperSnapshot.sharpe``, ``AdvisorScorecard``), that one conflict discards
both writes and surfaces as a 500. The window is real — the nightly job in
``jobs.py`` and a user-triggered recompute can hit the same portfolio.

The fix is the idiom ``get_or_create_paper_portfolio`` already uses for exactly
this reason, plus a SAVEPOINT so the conflict never poisons the outer
transaction the caller still intends to commit.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.models.entities import MetricsSnapshot


def upsert_metrics_snapshot(
    db: Session,
    *,
    portfolio_id: str,
    as_of: date,
    context: str,
    values: dict[str, Any],
) -> MetricsSnapshot:
    """Insert or update the snapshot row for ``(portfolio_id, as_of, context)``.

    Does not commit — the caller's own transaction still owns that, so the
    snapshot and the legacy columns it mirrors stay atomic with each other.
    """
    row = (
        db.query(MetricsSnapshot)
        .filter(
            MetricsSnapshot.portfolio_id == portfolio_id,
            MetricsSnapshot.as_of == as_of,
            MetricsSnapshot.context == context,
        )
        .one_or_none()
    )
    if row is None:
        row = MetricsSnapshot(portfolio_id=portfolio_id, as_of=as_of, context=context)
        for key, value in values.items():
            setattr(row, key, value)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            # A concurrent writer inserted the same key between our SELECT and
            # our INSERT. The SAVEPOINT rolled that back only; re-read and fall
            # through to the update path.
            row = (
                db.query(MetricsSnapshot)
                .filter(
                    MetricsSnapshot.portfolio_id == portfolio_id,
                    MetricsSnapshot.as_of == as_of,
                    MetricsSnapshot.context == context,
                )
                .one_or_none()
            )
            if row is None:
                raise
        else:
            return row

    for key, value in values.items():
        setattr(row, key, value)
    return row
