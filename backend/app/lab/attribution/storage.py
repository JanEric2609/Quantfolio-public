"""Persist attribution run results to database."""

import json
from datetime import date
from sqlalchemy.orm import Session
from app.foundation.models.entities import AttributionRun


def store_attribution_run(
    db: Session,
    user_id: str,
    kind: str,
    portfolio_id: str,
    benchmark: str,
    date_from: date,
    date_to: date,
    result_dict: dict,
) -> AttributionRun:
    """
    Store attribution run result.

    Args:
        db: Database session
        user_id: User ID
        kind: Attribution kind ("brinson" or "factor")
        portfolio_id: Portfolio ID
        benchmark: Benchmark ticker or composite ID
        date_from: Period start date
        date_to: Period end date
        result_dict: Attribution result (will be JSON-serialized)

    Returns:
        Stored AttributionRun entity
    """
    run = AttributionRun(
        user_id=user_id,
        kind=kind,
        portfolio_id=portfolio_id,
        benchmark=benchmark,
        date_from=date_from,
        date_to=date_to,
        result_json=json.dumps(result_dict, default=str),
    )
    db.add(run)
    db.flush()
    return run
