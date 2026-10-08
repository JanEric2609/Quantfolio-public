"""Calibration of a prediction's conviction into a hit probability (ADR 0018 §6).

A hit is beating the passive core ETF: ``excess_return > 0`` and nothing
else (rows resolved without a benchmark price are left out). Only matured
outcomes of the same source (Discover's composite, or one advisor sleeve's
confidence: different scales) are used, on an expanding window, and only
those resolved at least :data:`EMBARGO_TRADING_DAYS` trading days before the
new call was issued.

The number of independent labels is the number of **issue dates** (the picks
of one date share one market move), so the stages go by ``n_eff`` = distinct
issue dates:

* below :data:`MIN_N_EFF`: no probability at all (``conviction_calibrated``
  stays ``None``; the page shows the base rate and the score tier instead);
* up to :data:`ISOTONIC_N_EFF`: a logistic on the standardised score with a
  slope that may not be negative and an N(0, 1) prior on it, each row weighted
  1 / (calls on its date);
* above: weighted isotonic regression.

**Viability gate:** when the fitted probability moves by less than one
percentage point between the 10th and 90th score percentile, the score has no
usable signal and no probability is stored.

A monotone recalibration never changes ranking or resolution; it only makes
the stated number honest. Bump ``rule`` in :data:`CALIBRATOR_VERSION` with any
change here: it is stamped on every call's provenance.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from scipy.optimize import minimize
from sklearn.isotonic import IsotonicRegression
from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryPrediction

logger = logging.getLogger(__name__)

MIN_N_EFF = 100
ISOTONIC_N_EFF = 1000
EMBARGO_TRADING_DAYS = 21
SLOPE_PRIOR_SD = 1.0
#: Smallest P10-to-P90 change in the fitted probability that counts as signal.
MIN_SPREAD = 0.01

#: The calibration rule in force, stamped on every call's provenance (ADR 0018 §10).
CALIBRATOR_VERSION = {
    "method": "staged_adr0018",
    "hit": "excess>0",
    "min_n_eff": MIN_N_EFF,
    "isotonic_n_eff": ISOTONIC_N_EFF,
    "embargo_trading_days": EMBARGO_TRADING_DAYS,
    "slope_prior_sd": SLOPE_PRIOR_SD,
    "min_spread": MIN_SPREAD,
    "rule": 2,
}


@dataclass
class CalibrationFit:
    """One fit of the staged calibrator."""

    stage: str  # "base_rate" | "logistic" | "isotonic"
    n: int
    n_eff: int
    base_rate: float | None
    viable: bool = False
    spread: float | None = None
    params: dict[str, Any] = field(default_factory=dict)
    _iso: IsotonicRegression | None = None

    def predict(self, score: float) -> float | None:
        """Calibrated hit probability of *score*; ``None`` when no probability may be stated."""
        if not self.viable:
            return None
        if self.stage == "logistic":
            z = (score - self.params["mean"]) / self.params["sd"]
            return float(1.0 / (1.0 + np.exp(-(self.params["intercept"] + self.params["slope"] * z))))
        if self.stage == "isotonic" and self._iso is not None:
            return float(np.clip(self._iso.predict([score])[0], 0.0, 1.0))
        return None


def _fit_logistic(scores: np.ndarray, hits: np.ndarray, weights: np.ndarray) -> dict[str, float]:
    mean = float(scores.mean())
    sd = float(scores.std()) or 1.0
    z = (scores - mean) / sd

    def objective(theta: np.ndarray) -> tuple[float, np.ndarray]:
        a, b = theta
        eta = a + b * z
        p = 1.0 / (1.0 + np.exp(-eta))
        # Weighted negative log-likelihood (numerically stable) plus the slope prior.
        nll = float(np.sum(weights * (np.logaddexp(0.0, eta) - hits * eta)))
        resid = weights * (p - hits)
        grad = np.array([resid.sum(), (resid * z).sum() + b / SLOPE_PRIOR_SD**2])
        return nll + 0.5 * (b / SLOPE_PRIOR_SD) ** 2, grad

    res = minimize(objective, np.zeros(2), jac=True, method="L-BFGS-B", bounds=[(None, None), (0.0, None)])
    return {"intercept": float(res.x[0]), "slope": float(res.x[1]), "mean": mean, "sd": sd}


def fit_calibrator(rows: list[tuple[float, bool, date]]) -> CalibrationFit:
    """Fit from ``(score, hit, issue_date)`` rows; pure, no database."""
    n = len(rows)
    days = Counter(d for _, _, d in rows)
    n_eff = len(days)
    base_rate = float(np.mean([h for _, h, _ in rows])) if rows else None
    if n_eff < MIN_N_EFF:
        return CalibrationFit("base_rate", n, n_eff, base_rate)
    scores = np.asarray([s for s, _, _ in rows], dtype=float)
    hits = np.asarray([1.0 if h else 0.0 for _, h, _ in rows])
    weights = np.asarray([1.0 / days[d] for _, _, d in rows])
    p10, p90 = np.percentile(scores, [10, 90])
    if n_eff < ISOTONIC_N_EFF:
        fit = CalibrationFit("logistic", n, n_eff, base_rate, params=_fit_logistic(scores, hits, weights))
    else:
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
        iso.fit(scores, hits, sample_weight=weights)
        fit = CalibrationFit("isotonic", n, n_eff, base_rate, _iso=iso)
    fit.viable = True
    lo, hi = fit.predict(float(p10)), fit.predict(float(p90))
    fit.spread = abs(hi - lo) if lo is not None and hi is not None else None
    fit.viable = fit.spread is not None and fit.spread >= MIN_SPREAD
    return fit


def _embargo_cutoff(issued: datetime) -> datetime:
    day = np.busday_offset(np.datetime64(issued.date()), -EMBARGO_TRADING_DAYS, roll="backward")
    return datetime.combine(day.astype(object), datetime.min.time(), tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def training_rows(db: Session, prediction: DiscoveryPrediction) -> list[tuple[float, bool, date]]:
    """Matured, same-source, embargoed ``(score, hit, issue_date)`` rows for *prediction*."""
    issued = _utc(prediction.predicted_at or datetime.now(UTC))
    cutoff = _embargo_cutoff(issued)
    rows = (
        db.query(DiscoveryPrediction.conviction, DiscoveryPrediction.excess_return, DiscoveryPrediction.predicted_at,
                 DiscoveryPrediction.resolve_at)
        .filter(
            DiscoveryPrediction.user_id == prediction.user_id,
            DiscoveryPrediction.outcome_status.in_(("resolved", "delisted")),
            DiscoveryPrediction.conviction.isnot(None),
            DiscoveryPrediction.excess_return.isnot(None),
            DiscoveryPrediction.id != prediction.id,
            # Same source only: Discover's composite score and an advisor
            # sleeve's LLM confidence are different scales (scoring.ic_group).
            DiscoveryPrediction.portfolio_id.is_(None)
            if prediction.portfolio_id is None
            else DiscoveryPrediction.portfolio_id == prediction.portfolio_id,
        )
        .all()
    )
    out = []
    for conviction, excess, predicted_at, resolve_at in rows:
        if predicted_at is None or resolve_at is None or _utc(resolve_at) > cutoff:
            continue
        out.append((float(conviction), float(excess) > 0, _utc(predicted_at).date()))
    return out


def calibrate_prediction(db: Session, prediction_id: str, *, user_id: str) -> DiscoveryPrediction | None:
    """Set ``conviction_calibrated`` from the staged calibrator, or leave it ``None``.

    Returns the prediction (``None`` if it does not exist for *user_id*). The
    value is written once: the append-only trigger lets it go from NULL to a
    value and never change after.
    """
    prediction = db.query(DiscoveryPrediction).filter(
        DiscoveryPrediction.id == prediction_id,
        DiscoveryPrediction.user_id == user_id,
    ).first()
    if prediction is None:
        logger.warning("calibrator: prediction %s not found for user %s", prediction_id, user_id)
        return None
    if prediction.conviction is None or prediction.conviction_calibrated is not None:
        return prediction

    fit = fit_calibrator(training_rows(db, prediction))
    value = fit.predict(float(prediction.conviction))
    logger.debug(
        "calibrator: %s stage=%s n=%d n_eff=%d viable=%s -> %s",
        prediction_id, fit.stage, fit.n, fit.n_eff, fit.viable, value,
    )
    if value is not None:
        prediction.conviction_calibrated = value
        db.flush()
    return prediction


__all__ = ["CALIBRATOR_VERSION", "CalibrationFit", "calibrate_prediction", "fit_calibrator", "training_rows"]
