"""Assembles the Multi-Agent Council's context dict from quant + portfolio state.

Bridges ``quant_proposal`` (foundation-tier MC/skfolio trade-proposal builder)
and ``agents.orchestrator.CouncilOrchestrator``/``CompetitionOrchestrator``
(the council). Shared by ``llm_portfolio.review`` (mandate review, Phase A)
and ``advisor.evolution`` (champion/challenger competition, Phase B) — both
contexts reach it through the ``llm_portfolio`` facade, never this module
directly, per the facade-only import-linter contract.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoverCandidate, PaperHolding, PaperPortfolio, PaperSnapshot
from app.decision.llm_portfolio.context import _build_regime_block
from app.foundation.quant import correlation_matrix_from_price_matrix
from app.foundation.quant_proposal import TradeProposal, build_trade_proposal, concentration_from_weights
from app.foundation.settings import get_risk_free_rate

logger = logging.getLogger(__name__)


def _default_candidates(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    """Shortlisted Discover candidates, reshaped for build_trade_proposal.

    Used when the caller doesn't already have its own candidate set (e.g. the
    mandate review loop, which never built one before this wiring existed).
    """
    try:
        rows = (
            db.query(DiscoverCandidate)
            .filter(DiscoverCandidate.status == "shortlisted")
            .order_by(DiscoverCandidate.id.desc())
            .limit(limit)
            .all()
        )
        return [{"symbol": r.symbol, "name": r.name, "source": "discover"} for r in rows]
    except Exception as exc:
        logger.warning("council_context: discover shortlist unavailable: %s", exc)
        return []


def _cash_balance(db: Session, portfolio: PaperPortfolio) -> float:
    """Latest PaperSnapshot.cash_balance, falling back to initial_cash pre-snapshot."""
    latest = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio.id)
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    if latest is not None:
        return float(latest.cash_balance)
    return float(portfolio.initial_cash or 0)


def build_council_context(
    db: Session,
    portfolio: PaperPortfolio,
    *,
    strategy_prompt: str,
    candidates: list[dict[str, Any]] | None = None,
    trade_proposal: TradeProposal | None = None,
) -> dict[str, Any]:
    """Build the 18-key context dict CouncilOrchestrator.run_council(**context) expects.

    Args:
        db: Active session.
        portfolio: The PaperPortfolio the council evaluates.
        strategy_prompt: Mandate/strategy framing text (fed as ``strategy``).
        candidates: Discover-style candidates for the trade-proposal builder;
            defaults to the current shortlist when omitted.
        trade_proposal: Reuse an already-computed TradeProposal (e.g. one the
            caller built for its own cycle) instead of building a fresh one.

    Never raises for data gaps — every key degrades to a safe default and the
    council's own agents already tolerate thin/missing context per-agent.
    """
    if trade_proposal is None:
        trade_proposal = build_trade_proposal(
            db, portfolio.id, candidates if candidates is not None else _default_candidates(db)
        )

    holdings: dict[str, Any] = {
        ticker: {
            "quantity": qty,
            "market_value": trade_proposal.current_book.get(ticker, 0.0),
        }
        for ticker, qty in (
            (h.ticker.upper(), float(h.quantity))
            for h in db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio.id).all()
            if h.ticker
        )
    }
    cash = {"balance": _cash_balance(db, portfolio)}

    risk_envelope = trade_proposal.risk_envelope
    regime_block = _build_regime_block(db, portfolio.id)
    try:
        from app.lab.regime.macro_snapshot import get_or_refresh_regime

        macro_indicators = get_or_refresh_regime(db)
    except Exception as exc:
        logger.warning("council_context: macro snapshot unavailable: %s", exc)
        macro_indicators = {}

    return {
        "portfolio_id": portfolio.id,
        "holdings": holdings,
        "cash": cash,
        "market_data": {
            symbol: {"spot": summary.spot, "p50_return": summary.p50}
            for symbol, summary in trade_proposal.mc_summaries.items()
        },
        "performance": {
            "optimizer_status": trade_proposal.optimizer_status,
            "suggested_weights": trade_proposal.suggested_weights,
        },
        "discover_items": candidates if candidates is not None else _default_candidates(db),
        "var_95": risk_envelope.get("var_95_daily") or 0.0,
        "cvar_95": risk_envelope.get("cvar_95_daily") or 0.0,
        "max_drawdown": risk_envelope.get("max_drawdown") or 0.0,
        "concentration": {"hhi": concentration_from_weights(trade_proposal.suggested_weights)},
        "correlation_matrix": (
            correlation_matrix_from_price_matrix(trade_proposal.price_series)
            if trade_proposal.price_series
            else {}
        ),
        "risk_free_rate": get_risk_free_rate(db),
        "macro_indicators": macro_indicators,
        "sector_performance": {},
        "hmm_regime": regime_block.get("label"),
        "risk_budget": 1.0,
        "current_state": {"holdings": holdings, "cash": cash},
        "debate_feedback": None,
        "strategy": strategy_prompt,
    }
