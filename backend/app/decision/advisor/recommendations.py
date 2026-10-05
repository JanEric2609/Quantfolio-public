"""Graduated recommendation surface (PR2 E1).

``compute_what_would_change`` builds the position-level diff between the
champion's paper book and the user's REAL book: per name
``{real_weight, champion_weight, delta, action}`` ranked by
conviction × |delta|, each row carrying the champion's thesis and the
CALIBRATED confidence from the prediction ledger.

The former ``emit_rec_cards`` inbox writer was removed with the review
inbox (audit-fixes todo 8); recommendation surfacing remains on the
recommendations/dossier pages.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    DiscoveryPrediction,
    PaperHolding,
)
from app.decision.graduation.state import is_graduated
from app.foundation.live_positions import combined_positions
from app.foundation.market import latest_cached_close

logger = logging.getLogger(__name__)

# Minimum absolute weight delta — sub-1% shifts are noise, not advice.
REC_MIN_ABS_DELTA = 0.01

# Report Phase 4: only strategies with a passing evidence card set target
# weights for the real book, and they do it through the monthly plan. The LLM
# loop has no evidence card and stays paper-only (the owner's answer of
# 2026-09-24), so this diff is research whether or not the champion graduated.
RESEARCH_ONLY_NOTE = (
    "Research only: the LLM loop is paper-only and has no evidence card, so it never sets "
    "weights for your real book. Only the monthly plan does, from strategies that passed "
    "their evidence check."
)


def _real_book(db: Session, user_id: str) -> dict[str, dict[str, Any]]:
    """Real holdings at every broker by ticker (falling back to ISIN key) with EUR values.

    One row per instrument: the same ISIN at DKB and Scalable is summed under
    one key even when only one depot knows its ticker. Manual holdings are not
    read here, so a broker position awaiting reconciliation is kept.
    """
    book: dict[str, dict[str, Any]] = {}
    for pos in combined_positions(db, user_id, include_unreconciled=True):
        key = (pos.ticker or pos.isin or "").upper()
        if not key:
            continue
        entry = book.setdefault(
            key, {"ticker": pos.ticker, "isin": pos.isin, "name": pos.name, "value": 0.0}
        )
        entry["value"] += max(0.0, float(pos.value))
    return book


def _paper_book(db: Session, portfolio_id: str) -> dict[str, dict[str, Any]]:
    """Champion paper holdings by ticker with marked-to-market EUR values."""
    holdings = (
        db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio_id).all()
    )
    book: dict[str, dict[str, Any]] = {}
    for h in holdings:
        key = (h.ticker or h.isin or "").upper()
        if not key or h.quantity is None:
            continue
        price = latest_cached_close(db, h.ticker) if h.ticker else None
        if price is None:
            price = float(h.avg_buy_price or 0.0)
        value = float(h.quantity) * float(price)
        entry = book.setdefault(
            key, {"ticker": h.ticker, "isin": h.isin, "name": h.name, "value": 0.0}
        )
        entry["value"] += max(0.0, value)
    return book


def _latest_prediction(
    db: Session, user_id: str, portfolio_id: str, symbol: str
) -> DiscoveryPrediction | None:
    return (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.portfolio_id == portfolio_id,
            DiscoveryPrediction.symbol == symbol,
        )
        .order_by(DiscoveryPrediction.predicted_at.desc())
        .first()
    )


def _action_for(real_weight: float, delta: float) -> str:
    if real_weight <= 0 and delta > 0:
        return "new"
    if delta > 0:
        return "add"
    if real_weight > 0 and abs(delta) >= real_weight - 1e-9:
        return "exit"
    return "trim"


def compute_what_would_change(db: Session, user_id: str) -> dict[str, Any]:
    """Position-level "what would the champion change in YOUR book" diff (E1).

    Returns ``{graduated, champion_strategy_id, rows, ...}`` where rows are
    ranked by conviction × |delta|. Research only (``actionable`` is always
    False, see ``RESEARCH_ONLY_NOTE``), shown on the advisor's Divergence tab.
    """
    from app.decision.advisor.strategy import get_champion

    champion = get_champion(db, user_id)
    graduated = is_graduated(db, user_id)
    if champion is None or not champion.portfolio_id:
        return {
            "graduated": graduated,
            "champion_strategy_id": None,
            "rows": [],
            "note": "no champion strategy yet — run the advisor loop first",
            "actionable": False,
            "research_only": RESEARCH_ONLY_NOTE,
            "estimate": True,
            "not_financial_advice": True,
        }

    real = _real_book(db, user_id)
    paper = _paper_book(db, champion.portfolio_id)
    real_total = sum(e["value"] for e in real.values())
    paper_total = sum(e["value"] for e in paper.values())

    rows: list[dict[str, Any]] = []
    for key in sorted(set(real) | set(paper)):
        real_entry = real.get(key)
        paper_entry = paper.get(key)
        real_weight = (real_entry["value"] / real_total) if real_entry and real_total > 0 else 0.0
        champ_weight = (
            (paper_entry["value"] / paper_total) if paper_entry and paper_total > 0 else 0.0
        )
        delta = champ_weight - real_weight
        if abs(delta) < REC_MIN_ABS_DELTA:
            continue

        pred = _latest_prediction(db, user_id, champion.portfolio_id, key)
        confidence_raw = pred.conviction if pred else None
        confidence_cal = pred.conviction_calibrated if pred else None
        conviction = confidence_cal if confidence_cal is not None else (confidence_raw or 0.0)

        rows.append(
            {
                "ticker": key,
                "name": (paper_entry or real_entry or {}).get("name"),
                "isin": (paper_entry or real_entry or {}).get("isin"),
                "real_weight": round(real_weight, 4),
                "champion_weight": round(champ_weight, 4),
                "delta": round(delta, 4),
                "action": _action_for(real_weight, delta),
                "confidence_raw": confidence_raw,
                "confidence_calibrated": confidence_cal,
                "thesis": pred.thesis if pred else None,
                "mc": (
                    {
                        "p5": pred.expected_return_low,
                        "p50": pred.expected_return,
                        "p95": pred.expected_return_high,
                    }
                    if pred
                    else None
                ),
                "rank_score": round(float(conviction) * abs(delta), 6),
            }
        )

    rows.sort(key=lambda r: r["rank_score"], reverse=True)
    return {
        "graduated": graduated,
        "champion_strategy_id": champion.id,
        "champion_portfolio_id": champion.portfolio_id,
        "rows": rows,
        "real_total_eur": round(real_total, 2),
        "champion_total_eur": round(paper_total, 2),
        "actionable": False,
        "research_only": RESEARCH_ONLY_NOTE,
        "estimate": True,
        "not_financial_advice": True,
    }
