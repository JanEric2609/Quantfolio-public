"""Algorithmic-trading section (ADR 0029): a read-only overview. No order path, no write endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.foundation.auth import current_user
from app.foundation.models.entities import User
from app.lab.algo_lab import years_to_detect
from app.lab.algo_lab import cards as algo_cards

router = APIRouter(prefix="/api/algo", tags=["algo"])

SHARPES = (0.5, 1.0, 1.5)


@router.get("/overview")
def overview(user: User = Depends(current_user)) -> dict[str, Any]:
    """Draft cards, the candidates not carded, and how long a verdict would take. Nothing is funded."""
    return {
        "status": algo_cards.STATUS,
        "paper_only": True,
        "funded_candidates": 0,
        "family": algo_cards.FAMILY,
        "max_arms": algo_cards.MAX_ARMS,
        "cards": algo_cards.CARDS,
        "not_carded": algo_cards.NOT_CARDED,
        "years_to_detect": [
            {"sharpe": s, "years": round(years_to_detect(s, trials=algo_cards.MAX_ARMS, alt_se=True), 1),
             "years_single_arm": round(years_to_detect(s, alt_se=True), 1)}
            for s in SHARPES
        ],
        "note": "Research only. Intraday and event rules stay on the paper desk as measured nulls (ADR 0027, 0028).",
    }
