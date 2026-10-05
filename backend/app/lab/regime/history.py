"""Daily regime label ledger (one row per day, written by the daily job).

``regime_snapshots`` keeps the raw job output; this keeps the *call*: the
label the model issued on a given day, frozen so it can be scored against what
the market did afterwards ("Can I trust it?" page). Scoring is deliberately
not implemented here: the regime labels are latent volatility states
(low vol = bull, high vol = bear) with no realised-state definition in this
package to grade them against, so the history only accumulates until one is
agreed.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import RegimeLabelHistory

logger = logging.getLogger(__name__)


def record_regime_label(
    db: Session,
    *,
    as_of: date,
    label: str,
    probs: dict[str, Any] | None,
    model: str,
) -> RegimeLabelHistory | None:
    """Upsert the regime call for *as_of* and commit; never raises.

    A second run on the same UTC day replaces the first (the job re-ran
    before the horizon, nothing had been scored). Returns ``None`` when the
    row could not be written, so the snapshot write that precedes this is
    never failed by the ledger.
    """
    try:
        clean_probs = {str(k): float(v) for k, v in (probs or {}).items()}
        row = db.query(RegimeLabelHistory).filter(RegimeLabelHistory.as_of == as_of).one_or_none()
        if row is None:
            row = RegimeLabelHistory(as_of=as_of)
            db.add(row)
        row.label = str(label)
        row.probabilities_json = clean_probs
        row.model = str(model)
        db.commit()
        return row
    except Exception:
        logger.exception("regime_label_history: write failed for %s", as_of)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - session already unusable
            logger.warning("regime_label_history: rollback failed", exc_info=True)
        return None
