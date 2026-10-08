"""Forward predictor for Discovery pipeline (P0 — #111).

Stores a single ``DiscoveryPrediction`` row per shortlist candidate and provides
a guard function to determine whether an LLM dossier should be generated for a
prediction that currently has no thesis content.

The prediction captures point-in-time signal snapshots.  Calibrated-conviction
and expected-return fields are left ``NULL`` — they are filled later by the P2
calibrator and LLM predictor.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryPrediction
from app.foundation.models.entities._core import now_utc
from app.decision.discover.ledger import DEFAULT_HORIZON_DAYS, add_trading_days
from app.foundation.market import latest_cached_close

logger = logging.getLogger(__name__)


VALID_DIRECTIONS = frozenset({"buy", "sell", "neutral"})


def store_prediction(
    db: Session,
    *,
    symbol: str,
    composite_score: float,
    signal_breakdown: dict[str, Any],
    direction_hint: str = "buy",
    user_id: str,
    run_id: str,
    isin: str | None = None,
    config_id: str | None = None,
    horizon_days: int | None = None,
    expected_return: float | None = None,
    expected_return_low: float | None = None,
    expected_return_high: float | None = None,
    thesis: str | None = None,
    mc_prob_positive: float | None = None,
    price_at_prediction: float | None = None,
    portfolio_id: str | None = None,
    provenance_json: dict[str, Any] | None = None,
) -> DiscoveryPrediction:
    """Create and persist a single forward-prediction ledger row.

    Args:
        db: Active session.  The row is flushed but **not** committed — the
            caller owns the transaction.
        symbol: Ticker symbol.
        composite_score: Raw composite signal score (0–1).  Clamped to [0, 1].
        signal_breakdown: Per-stage signal dict stored under
            ``features_json["signal_breakdown"]``.
        direction_hint:  ``"buy"`` / ``"sell"`` / ``"neutral"``.
        user_id:  Owner of the discovery run.
        run_id:   DiscoverRun.id that this prediction belongs to.
        isin:     Optional ISIN.
        config_id: Optional DiscoveryConfig.id that produced the signal weights.
        provenance_json: What produced the call (ADR 0018 §10). Written with
            the row and append-only afterwards (Postgres trigger).

    Returns:
        The newly created ``DiscoveryPrediction`` (already added to *db*).

    """
    if direction_hint not in VALID_DIRECTIONS:
        logger.warning(
            "store_prediction: unknown direction %r, falling back to 'buy'",
            direction_hint,
        )
        direction_hint = "buy"

    predicted_at = now_utc()
    horizon = int(horizon_days) if horizon_days is not None else DEFAULT_HORIZON_DAYS
    resolve_at = add_trading_days(predicted_at, horizon)

    conviction = None
    if composite_score is not None:
        conviction = max(0.0, min(1.0, float(composite_score)))

    price = (
        price_at_prediction if price_at_prediction is not None else latest_cached_close(db, symbol)
    )

    features: dict[str, Any] = {
        "signal_breakdown": signal_breakdown,
    }
    if mc_prob_positive is not None:
        features["mc_prob_positive"] = float(mc_prob_positive)

    prediction = DiscoveryPrediction(
        user_id=user_id,
        run_id=run_id,
        symbol=symbol,
        isin=isin,
        predicted_at=predicted_at,
        horizon_days=horizon,
        resolve_at=resolve_at,
        direction=direction_hint,
        conviction=conviction,
        conviction_calibrated=None,
        expected_return=expected_return,
        expected_return_low=expected_return_low,
        expected_return_high=expected_return_high,
        thesis=thesis,
        price_at_prediction=price,
        features_json=features,
        outcome_status="pending",
        is_estimate=True,
        is_financial_advice=False,
        config_id=config_id,
        portfolio_id=portfolio_id,
        provenance_json=provenance_json,
    )
    db.add(prediction)
    db.flush()
    logger.debug(
        "store_prediction: created %s for %s (score=%.3f, direction=%s)",
        prediction.id,
        symbol,
        composite_score,
        direction_hint,
    )
    return prediction


def predictor_should_generate_dossier(prediction_id: str, db: Session) -> bool:
    """Return ``True`` if the prediction exists and has no dossier content yet.

    The check is: the row exists **and** ``thesis`` is ``NULL``.  If the row
    is missing or ``thesis`` is already populated the function returns
    ``False`` so the caller can skip dossier generation.

    Args:
        prediction_id: ``DiscoveryPrediction.id`` to inspect.
        db: Active session.

    Returns:
        ``True`` when an LLM dossier should be generated.
    """
    prediction = db.get(DiscoveryPrediction, prediction_id)
    if prediction is None:
        return False
    return prediction.thesis is None
