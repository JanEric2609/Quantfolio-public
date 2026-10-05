"""Discovery prediction calibrator (P2 — Task 3).

Calibrates raw conviction scores via a logistic (Platt-scaling) fit against
historical resolved predictions — despite this module's naming history
("Mincer regression"), it fits hit probability against conviction, not a
Mincer-Zarnowitz regression (F14; see
app.decision.discover.scoring._fit_hit_probability_logit and
app.foundation.quant_metrics.compute_mincer_zarnowitz for the real MZ
regression).

Without a fit (fewer than ``min_rows`` resolved rows from the same source,
or a failed fit) ``conviction_calibrated`` is ``None``. It used to be the raw
score itself, so every prediction on prod carried "calibrated" == raw: the
advisor showed "0.65 -> 0.65" where its UI has an "uncalibrated" state, and
scoring labelled raw-score Brier values ``brier_basis: "calibrated"``.
Callers fall back to the raw conviction and say so.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryPrediction
from app.decision.discover.scoring import _fit_hit_probability_logit

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp *value* to the inclusive range [*lo*, *hi*]."""
    return float(max(lo, float(min(hi, value))))


def calibrate_prediction(
    db: Session,
    prediction_id: str,
    *,
    user_id: str,
    lookback_days: int = 365,
    min_rows: int = 20,
) -> DiscoveryPrediction | None:
    """Calibrate raw conviction for a single prediction using a logistic
    (Platt-scaling) fit against historical resolved predictions.

    Args:
        db: Active SQLAlchemy session.
        prediction_id: UUID of the prediction to calibrate.
        user_id: Only calibrate from this user's resolved predictions.
        lookback_days: Window for resolved prediction history (default 365).
        min_rows: Minimum resolved predictions to run the fit (default 20).

    Returns:
        The updated ``DiscoveryPrediction`` with ``conviction_calibrated`` set,
        or ``None`` if the prediction does not exist.
    """
    # 1. Fetch the prediction — fail fast if missing.
    prediction = db.query(DiscoveryPrediction).filter(
        DiscoveryPrediction.id == prediction_id,
        DiscoveryPrediction.user_id == user_id,
    ).first()
    if prediction is None:
        logger.warning("calibrator: prediction %s not found for user %s", prediction_id, user_id)
        return None

    if prediction.conviction is None:
        # No raw conviction to calibrate.
        prediction.conviction_calibrated = None
        db.flush()
        return prediction

    # 2. Query resolved predictions for this user within the lookback window.
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    valid_rows = (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.outcome_status == "resolved",
            DiscoveryPrediction.predicted_at >= cutoff,
            DiscoveryPrediction.conviction.isnot(None),
            DiscoveryPrediction.realised_return.isnot(None),
            DiscoveryPrediction.id != prediction_id,  # exclude self
            # Same source only: Discover's composite score and an advisor
            # sleeve's LLM confidence are different scales (scoring.ic_group).
            DiscoveryPrediction.portfolio_id.is_(None)
            if prediction.portfolio_id is None
            else DiscoveryPrediction.portfolio_id == prediction.portfolio_id,
        )
        .order_by(DiscoveryPrediction.predicted_at)
        .all()
    )

    # 3. Convert to dicts for _fit_hit_probability_logit.
    valid_dicts = [
        {
            "conviction": r.conviction,
            "realised_return": r.realised_return,
            "excess_return": r.excess_return,
        }
        for r in valid_rows
    ]

    # 4. Fit the logistic hit-probability model; without one, no value.
    calibrated: float | None
    if len(valid_dicts) >= min_rows:
        a0, a1 = _fit_hit_probability_logit(valid_dicts)
        if a0 is not None and a1 is not None:
            raw_logit = a0 + a1 * prediction.conviction
            calibrated = _clamp(float(1.0 / (1.0 + np.exp(-raw_logit))))
            logger.debug(
                "calibrator: logit fit for %s (a0=%.4f, a1=%.4f, raw=%.3f -> calibrated=%s)",
                prediction_id, a0, a1, prediction.conviction, calibrated,
            )
        else:
            logger.warning(
                "calibrator: logit fit failed for %s (a0=%s, a1=%s); left uncalibrated",
                prediction_id, a0, a1,
            )
            calibrated = None
    else:
        calibrated = None
        logger.debug(
            "calibrator: %s left uncalibrated (n=%d < min_rows=%d, raw=%.3f)",
            prediction_id, len(valid_dicts), min_rows, prediction.conviction,
        )

    # 5. Persist and return.
    prediction.conviction_calibrated = calibrated
    db.flush()
    return prediction
