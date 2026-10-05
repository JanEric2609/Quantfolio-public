"""Mirror real DKB holdings into a paper portfolio for LLM mandate management."""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.live_positions import synced_cash
from app.foundation.models.entities import PaperPortfolio
from app.foundation.models.entities._core import now_utc

logger = logging.getLogger(__name__)

_MANDATE_CONFIGS: dict[str, dict[str, Any]] = {
    "A": {
        "style": "balanced_improver",
        "max_single_position_pct": 0.15,
        "min_etf_pct": 0.40,
        "rebalance_threshold": 0.10,
    },
    "B": {
        "style": "momentum_tilted",
        "max_single_position_pct": 0.20,
        "min_etf_pct": 0.30,
        "momentum_lookback_months": 6,
        "rebalance_threshold": 0.15,
    },
}

_MANDATE_NAMES: dict[str, str] = {
    "A": "Mandate A — Balanced Improver",
    "B": "Mandate B — Momentum Tilted",
}


def ensure_mandate_portfolio(
    db: Session,
    user_id: str,
    mandate: str,
) -> PaperPortfolio:
    """Find or create a PaperPortfolio matching user_id + mandate.

    Args:
        db: Database session.
        user_id: User UUID.
        mandate: Mandate identifier ("A" or "B").

    Returns:
        The existing or newly created PaperPortfolio.
    """
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == mandate)
        .one_or_none()
    )

    created = portfolio is None
    if portfolio is None:
        # Sum ONLY cash/current accounts — exclude depot (securities) accounts whose
        # balance reflects the market value of holdings, not spendable cash.
        total_cash, _ = synced_cash(db, user_id)

        name = _MANDATE_NAMES.get(mandate, f"Mandate {mandate}")
        config = _MANDATE_CONFIGS.get(mandate, {})

        portfolio = PaperPortfolio(
            user_id=user_id,
            name=name,
            initial_cash=total_cash,
            managed_by="llm",
            mandate=mandate,
            mandate_config_json=json.dumps(config),
            inception_at=now_utc(),
        )
        db.add(portfolio)
        db.commit()
        db.refresh(portfolio)
        logger.info("Created paper portfolio %s for user %s mandate %s", portfolio.id, user_id, mandate)

    # Seed only a portfolio created in this call. A sleeve that sold every
    # line is still a run in progress; re-seeding it from the real book (the
    # old "no holdings" test) silently added free securities mid-run. A fresh
    # start is reset_portfolio's job.
    freshly_seeded = False
    if created:
        from app.decision.paper_portfolio import _seed_holdings

        _seed_holdings(db, portfolio.id, user_id)
        freshly_seeded = True

    # Recompute baseline_value only when the portfolio genuinely never had a
    # baseline (heals legacy rows created before holdings were seeded, or
    # before the Bug-1 depot-exclusion fix) or when holdings were just seeded
    # from empty in this same call. Once a portfolio has a real baseline
    # established at genuine inception, it must stay pinned regardless of
    # subsequent trades — recomputing it from whatever holdings currently
    # exist would silently overwrite the starting-capital denominator every
    # time holdings change (e.g. after a sell), corrupting total_return_pct.
    if portfolio.baseline_value is None or freshly_seeded:
        from app.decision.paper_portfolio import recompute_baseline_value

        recompute_baseline_value(db, portfolio.id)
        db.refresh(portfolio)

    return portfolio
