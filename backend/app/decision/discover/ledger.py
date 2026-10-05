"""Discovery prediction ledger (P0 — #111).

Writes one immutable ``DiscoveryPrediction`` row per shortlist candidate on
each discovery run, capturing the point-in-time signal snapshot and price. This
is the data-collection spine that all later phases (resolution scoring,
calibration, self-improvement) depend on.

No behaviour change to discovery itself — predictions are written as a side
effect after dossiers are generated. At P0 the forward-predictor fields
(calibrated conviction, expected-return range, thesis/risks) are left NULL;
``conviction`` holds the raw composite signal score so each row is meaningful.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryPrediction
from app.foundation.models.entities._core import now_utc
from app.foundation.market import latest_cached_close

logger = logging.getLogger(__name__)

# Advisor-loop PR1: predictions are scored 21 TRADING days (~1 month) after
# they are logged. Config-overridable via env; resolve_at is computed by
# counting trading days (weekends skipped), not calendar days.
DEFAULT_HORIZON_DAYS = int(os.getenv("DISCOVER_PREDICTION_HORIZON_TRADING_DAYS") or "21")


def add_trading_days(start: datetime, trading_days: int) -> datetime:
    """Return *start* advanced by *trading_days* week-days (Sat/Sun skipped).

    Market holidays are not modelled here — the outcome evaluator resolves at
    the first available close on/after ``resolve_at``, which absorbs holidays.
    """
    current = start
    remaining = int(trading_days)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def write_predictions(
    db: Session,
    *,
    run_id: str,
    user_id: str,
    shortlisted: list[dict[str, Any]],
    config_id: str | None = None,
) -> list[DiscoveryPrediction]:
    """Persist a prediction-ledger row per shortlist candidate.

    Args:
        db: Active session (caller commits via this session's transaction).
        run_id: DiscoverRun id — groups this batch of predictions.
        user_id: Owner of the run.
        shortlisted: Per-candidate pipeline result dicts (``symbol``, ``isin``,
            ``scores``, ``composite_score``, ``concerns`` …).
        config_id: Optional DiscoveryConfig.id.

    Returns:
        The list of created ``DiscoveryPrediction`` rows (already added to the
        session and committed).
    """
    predicted_at = now_utc()
    resolve_at = add_trading_days(predicted_at, DEFAULT_HORIZON_DAYS)
    created: list[DiscoveryPrediction] = []

    for result in shortlisted:
        symbol = result.get("symbol")
        if not symbol:
            continue

        composite = result.get("composite_score")
        conviction = None
        if composite is not None:
            # Composite is already bounded to 0–1; clamp defensively.
            conviction = max(0.0, min(1.0, float(composite)))

        features = {
            "scores": result.get("scores", {}),
            "composite_score": composite,
            "concerns": result.get("concerns", []),
            "source": result.get("source"),
        }

        pred = DiscoveryPrediction(
            id=str(uuid.uuid4()),
            user_id=user_id,
            run_id=run_id,
            symbol=symbol,
            isin=result.get("isin"),
            predicted_at=predicted_at,
            horizon_days=DEFAULT_HORIZON_DAYS,
            resolve_at=resolve_at,
            direction="buy",
            conviction=conviction,
            features_json=features,
            price_at_prediction=latest_cached_close(db, symbol),
            outcome_status="pending",
            config_id=config_id,
        )
        db.add(pred)
        created.append(pred)

    if created:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        logger.info(
            "ledger: wrote %d prediction rows for run %s", len(created), run_id
        )
    return created
