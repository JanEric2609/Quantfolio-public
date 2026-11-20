"""Read the latest validated RL portfolio-allocation signal (Track D2).

Pure DB read — never trains, never rolls out. ``advisor/cycle.py`` calls
this right after ``build_trade_proposal`` to get one more advisory input
for ``decide_trades``; the scheduled ``advisor_rl_training`` job
(``advisor/jobs.py``, via ``advisor.rl_training.train_and_validate_policy``)
is the only thing that ever writes a ``QuantRlPolicy`` row this reads.

Fails open (returns ``None``) whenever there's no validated policy yet, or
its persisted ``final_weights`` can't be parsed — a missing/broken RL
signal must never block or distort a paper-trading cycle that would
otherwise run fine without it, matching every other gated signal in this
codebase (fundamentals, sentiment, ic_icir, regime, ml_signal).
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def latest_rl_portfolio_signal(
    db: Any,
    user_id: str,
    env_id: str = "portfolio_allocation",
) -> dict[str, float] | None:
    """Latest validated policy's rollout-derived final weights for *user_id*.

    Returns ``None`` when no ``QuantRlPolicy`` row has cleared the
    ``train_and_validate_policy`` gate (``status == "completed"``) or its
    persisted weights can't be parsed.
    """
    from app.foundation.models.entities import QuantRlPolicy

    row = (
        db.query(QuantRlPolicy)
        .filter(
            QuantRlPolicy.user_id == user_id,
            QuantRlPolicy.env_id == env_id,
            QuantRlPolicy.status == "completed",
        )
        .order_by(QuantRlPolicy.finished_at.desc())
        .first()
    )
    if row is None:
        return None

    try:
        metrics = json.loads(row.training_metrics_json or "{}")
    except (TypeError, ValueError):
        logger.warning("rl_signal: policy %s has unparsable training_metrics_json", row.id)
        return None

    weights = metrics.get("final_weights")
    if not isinstance(weights, dict) or not weights:
        return None
    try:
        return {str(k): float(v) for k, v in weights.items()}
    except (TypeError, ValueError):
        logger.warning("rl_signal: policy %s has malformed final_weights", row.id)
        return None
