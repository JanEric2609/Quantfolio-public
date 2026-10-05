"""Regime-aware advisor with Multiplicative Weights Update (MWU) for closed-loop refinement.

Actions tracked per regime label:
  - buy_equity        — recommendation to increase equity exposure
  - buy_bond          — recommendation to increase bond/fixed-income exposure
  - reduce_holding    — recommendation to reduce/trim a position
  - hold_cash         — recommendation to hold cash / reduce risk
  - sector_rotate     — recommendation to rotate sectors

The MWU algorithm:
  1. Initialize all actions with equal weight (1.0 / num_actions)
  2. On each outcome evaluation, compute loss per action:
       loss = 0.0 on correct, 0.5 on neutral, 1.0 on incorrect
  3. New weight = old_weight * (1 - eta * loss)
  4. Normalize weights so they sum to 1.0
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    FactorIcTracking,
    Recommendation,
    RecommendationOutcome,
    RegimeRecommendationWeight,
)

# Actions tracked by the MWU system
MWU_ACTIONS = [
    "buy_equity",
    "buy_bond",
    "reduce_holding",
    "hold_cash",
    "sector_rotate",
]

LEARNING_RATE = 0.1  # eta — how quickly weights adapt
REGIME_LABELS = ["bull", "bear", "high_vol", "low_vol", "transition"]


# --- MWU Weight Management ---


def _accuracy_for_label(label: str) -> float:
    """Convert outcome label to accuracy in [0, 1]."""
    if label == "correct":
        return 1.0
    if label == "incorrect":
        return 0.0
    if label == "neutral":
        return 0.5
    raise ValueError(f"Unexpected outcome label: {label}")


def _loss_for_label(label: str) -> float:
    """Convert outcome label to loss in [0, 1] for MWU update."""
    return 1.0 - _accuracy_for_label(label)


def _infer_action_from_rec(rec: Recommendation) -> str:
    """Map a recommendation verdict/ticker to an MWU action."""
    verdict = (rec.verdict or "").upper()

    if verdict in ("BUY", "STRONG_BUY", "BULLISH", "OUTPERFORM"):
        return "buy_equity"
    if verdict in ("SELL", "STRONG_SELL", "BEARISH", "UNDERPERFORM"):
        return "reduce_holding"
    if verdict in ("HOLD", "NEUTRAL", "MARKET_WEIGHT", "WATCH"):
        return "hold_cash"
    if "BOND" in verdict or "BOND" in (rec.horizon or "").upper():
        return "buy_bond"
    if verdict in ("ROTATE", "SECTOR_ROTATE", "OVERWEIGHT"):
        return "sector_rotate"

    # Fallback: infer from payload
    try:
        payload = json.loads(rec.payload_json or "{}")
        checks = payload.get("checks", [])
        for c in checks:
            msg = (c.get("message", "") or "").lower()
            if "reduce" in msg or "trim" in msg:
                return "reduce_holding"
            if "bond" in msg or "fixed income" in msg:
                return "buy_bond"
            if "cash" in msg or "defensive" in msg:
                return "hold_cash"
            if "rotate" in msg or "sector" in msg:
                return "sector_rotate"
            if "equity" in msg or "stock" in msg:
                return "buy_equity"
    except Exception:
        pass

    return "buy_equity"  # safest default


def _infer_regime_from_rec(rec: Recommendation, default_regime: str = "unknown") -> str:
    """Extract the regime label from a recommendation's payload."""
    try:
        payload = json.loads(rec.payload_json or "{}")
        regime = payload.get("regime") or {}
        return regime.get("label", default_regime)
    except Exception:
        return default_regime


def get_or_init_weights(db: Session, regime_label: str) -> dict[str, float]:
    """Return current weights for a regime, initializing if missing."""
    rows: list[RegimeRecommendationWeight] = (
        db.query(RegimeRecommendationWeight)
        .filter(RegimeRecommendationWeight.regime_label == regime_label)
        .all()
    )
    existing = {r.action: r.weight for r in rows}

    # Ensure all actions exist
    for action in MWU_ACTIONS:
        if action not in existing:
            w = RegimeRecommendationWeight(
                regime_label=regime_label,
                action=action,
                weight=1.0 / len(MWU_ACTIONS),
                n_obs=0,
            )
            db.add(w)
            existing[action] = 1.0 / len(MWU_ACTIONS)
    db.commit()

    return existing


def update_weights_for_outcome(
    db: Session,
    outcome: RecommendationOutcome,
    rec: Recommendation,
) -> Optional[dict[str, float]]:
    """Update MWU weights for a single evaluated outcome.

    Returns the updated weight dict for the relevant regime, or None if unresolvable.
    """
    if outcome.outcome_label in ("pending", "unresolvable"):
        return None

    regime_label = _infer_regime_from_rec(rec)
    action = _infer_action_from_rec(rec)
    loss = _loss_for_label(outcome.outcome_label)

    # Ensure all action rows exist before updating, so normalization covers all actions
    get_or_init_weights(db, regime_label)

    row: RegimeRecommendationWeight | None = (
        db.query(RegimeRecommendationWeight)
        .filter(
            RegimeRecommendationWeight.regime_label == regime_label,
            RegimeRecommendationWeight.action == action,
        )
        .first()
    )
    if row is None:
        raise ValueError(
            f"RegimeRecommendationWeight not found for regime_label={regime_label!r}, "
            f"action={action!r} after get_or_init_weights() initialization. "
            "This indicates a bug in weight initialization."
        )

    # Track accuracy
    accuracy = _accuracy_for_label(outcome.outcome_label)
    old_cum = row.cum_accuracy or 0.0
    row.n_obs = (row.n_obs or 0) + 1
    row.cum_accuracy = (old_cum * (row.n_obs - 1) + accuracy) / row.n_obs

    # MWU update: weight *= (1 - eta * loss)
    row.weight = row.weight * (1.0 - LEARNING_RATE * loss)
    row.updated_at = datetime.now(UTC)

    # Normalize all weights for this regime
    all_rows: list[RegimeRecommendationWeight] = (
        db.query(RegimeRecommendationWeight)
        .filter(RegimeRecommendationWeight.regime_label == regime_label)
        .all()
    )
    total_w = sum(r.weight for r in all_rows)
    if total_w > 0:
        for r in all_rows:
            r.weight = r.weight / total_w

    db.commit()
    return {r.action: r.weight for r in all_rows}


def run_mwu_update(db: Session) -> int:
    """Run MWU update across all evaluated outcomes that haven't been applied yet.

    Uses notes_json to track which outcomes have been processed.
    Returns count of outcomes processed.
    """
    updates = 0
    now = datetime.now(UTC)

    outcomes: list[RecommendationOutcome] = (
        db.query(RecommendationOutcome)
        .filter(RecommendationOutcome.outcome_label.in_(["correct", "incorrect", "neutral"]))
        .all()
    )

    for outcome in outcomes:
        rec: Recommendation | None = (
            db.query(Recommendation)
            .filter(Recommendation.id == outcome.recommendation_id)
            .first()
        )
        if not rec:
            continue

        # Check if MWU was already applied
        existing_notes = outcomes_already_applied(outcome)
        if existing_notes:
            continue

        result = update_weights_for_outcome(db, outcome, rec)
        if result is not None:
            # Mark as applied in notes_json
            notes = json.loads(outcome.notes_json or "{}")
            notes["mwu_applied_at"] = now.isoformat()
            outcome.notes_json = json.dumps(notes)
            updates += 1

    db.commit()
    return updates


def outcomes_already_applied(outcome: RecommendationOutcome) -> bool:
    """Check if MWU has already been applied to this outcome."""
    try:
        notes = json.loads(outcome.notes_json or "{}")
        return "mwu_applied_at" in notes
    except Exception:
        return False


def get_weights_summary(db: Session) -> list[dict]:
    """Get all regime weights for display."""
    rows: list[RegimeRecommendationWeight] = (
        db.query(RegimeRecommendationWeight)
        .order_by(RegimeRecommendationWeight.regime_label, RegimeRecommendationWeight.action)
        .all()
    )
    return [
        {
            "regime_label": r.regime_label,
            "action": r.action,
            "weight": round(r.weight, 4),
            "n_obs": r.n_obs,
            "cum_accuracy": round(r.cum_accuracy, 4) if r.cum_accuracy is not None else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


# --- Factor IC Tracking ---


FACTORS = ["momentum", "value", "size", "quality", "low_vol", "yield"]


def compute_factor_ic(
    outcomes: list[tuple[RecommendationOutcome, Recommendation]],
    regime_label: str,
    factor_name: str,
) -> Optional[dict]:
    """Compute Information Coefficient (rank correlation between predicted score and actual return).

    For Phase 4, we use benchmark_excess_return as the 'actual' and
    infer predicted score from the recommendation confidence + payload data.

    Returns dict with ic_value, t_stat, n_obs or None if insufficient data.
    """

    pairs = []
    for outcome, rec in outcomes:
        if outcome.benchmark_excess_return is None:
            continue
        # Infer predicted score: higher confidence = stronger prediction
        pred_score = float(rec.confidence) if rec.confidence else 0.5
        # Adjust by factor alignment (simplified: regime context)
        try:
            payload = json.loads(rec.payload_json or "{}")
            checks = payload.get("checks", [])
            # Factor alignment: does the recommendation mention this factor?
            factor_alignment = 0.0
            for c in checks:
                msg = ((c.get("message", "") or "") + " " + (c.get("detail", "") or "")).lower()
                if factor_name in msg:
                    factor_alignment = 0.3
            pred_score = min(1.0, pred_score + factor_alignment)
        except Exception:
            pass
        pairs.append((pred_score, outcome.benchmark_excess_return))

    if len(pairs) < 5:
        return None

    # Spearman rank correlation via Pearson on ranks (handles ties correctly)
    n = len(pairs)
    pred_ranks = _rank([p[0] for p in pairs])
    actual_ranks = _rank([p[1] for p in pairs])

    mean_pr = sum(pred_ranks) / n
    mean_ar = sum(actual_ranks) / n
    cov = sum((pr - mean_pr) * (ar - mean_ar) for pr, ar in zip(pred_ranks, actual_ranks)) / n
    std_pr = math.sqrt(sum((pr - mean_pr) ** 2 for pr in pred_ranks) / n)
    std_ar = math.sqrt(sum((ar - mean_ar) ** 2 for ar in actual_ranks) / n)
    if std_pr == 0 or std_ar == 0:
        return None
    rho = max(-1.0, min(1.0, cov / (std_pr * std_ar)))

    # t-statistic for significance test
    denom = math.sqrt(max((1.0 - rho * rho) / max(n - 2, 1), 0.0))
    t_stat = rho / denom if denom > 0 else None

    return {
        "ic_value": round(rho, 4),
        "t_stat": round(t_stat, 4) if t_stat is not None else None,
        "n_obs": n,
    }


def _rank(values: list[float]) -> list[float]:
    """Compute rank (1..n) for a list of values, handling ties via average rank."""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        # Find ties
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0  # average of ranks from i+1 to j
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def update_factor_ic_tracking(db: Session) -> int:
    """Update FactorIcTracking for all regime × factor combinations."""

    updates = 0
    all_outcomes = (
        db.query(RecommendationOutcome, Recommendation)
        .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
        .filter(
            RecommendationOutcome.outcome_label.in_(["correct", "incorrect", "neutral"]),
            RecommendationOutcome.benchmark_excess_return.isnot(None),
        )
        .all()
    )

    for regime_label in REGIME_LABELS:
        outcomes = [
            (outcome, rec) for outcome, rec in all_outcomes
            if _infer_regime_from_rec(rec) == regime_label
        ]

        if not outcomes:
            continue

        for factor_name in FACTORS:
            result = compute_factor_ic(outcomes, regime_label, factor_name)
            if result is None:
                continue

            # Upsert
            existing: FactorIcTracking | None = (
                db.query(FactorIcTracking)
                .filter(
                    FactorIcTracking.regime_label == regime_label,
                    FactorIcTracking.factor_name == factor_name,
                )
                .first()
            )
            if existing:
                existing.ic_value = result["ic_value"]
                existing.t_stat = result["t_stat"]
                existing.n_obs = result["n_obs"]
                existing.evaluated_at = datetime.now(UTC)
            else:
                tracking = FactorIcTracking(
                    regime_label=regime_label,
                    factor_name=factor_name,
                    ic_value=result["ic_value"],
                    t_stat=result["t_stat"],
                    n_obs=result["n_obs"],
                    evaluated_at=datetime.now(UTC),
                )
                db.add(tracking)
            updates += 1

    db.commit()
    return updates


def get_factor_ics(db: Session) -> list[dict]:
    """Get all factor IC tracking records for display."""
    rows: list[FactorIcTracking] = (
        db.query(FactorIcTracking)
        .order_by(FactorIcTracking.regime_label, FactorIcTracking.factor_name)
        .all()
    )
    return [
        {
            "regime_label": r.regime_label,
            "factor_name": r.factor_name,
            "ic_value": round(r.ic_value, 4),
            "t_stat": round(r.t_stat, 4) if r.t_stat is not None else None,
            "n_obs": r.n_obs,
            "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
        }
        for r in rows
    ]


# --- Wiring into advisor ---


def get_regime_adjusted_weights(db: Session, regime_label: str) -> dict[str, float]:
    """Get the current MWU weights to use as context in advisor prompts.

    Returns dict of action → weight for the given regime.
    """
    weights = get_or_init_weights(db, regime_label)
    return weights
