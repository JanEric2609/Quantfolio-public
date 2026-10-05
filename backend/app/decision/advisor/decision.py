"""Re-export shim — moved to ``app.foundation.quant_proposal`` (foundation-tier).

This module's contents (MC + skfolio trade-proposal builder) had no
decision-loop imports of its own, so they were relocated to a foundation-tier
module that ``llm_portfolio`` can also depend on directly without tripping
the "decision-loop internals are facade-only" import-linter contract. Kept
here as a transparent re-export so existing call sites
(``advisor/cycle.py``, ``advisor/llm_decision.py``, ``advisor/risk_gate.py``)
and tests are unaffected.
"""
from __future__ import annotations

from app.foundation.quant_proposal import (
    MC_HORIZON_TRADING_DAYS,
    MC_N_PATHS,
    McSummary,
    TradeProposal,
    build_trade_proposal,
    concentration_from_weights,
)
from app.foundation.quant_proposal import _portfolio_risk_envelope  # noqa: F401 — re-exported for risk_gate.py

__all__ = [
    "MC_HORIZON_TRADING_DAYS",
    "MC_N_PATHS",
    "McSummary",
    "TradeProposal",
    "build_trade_proposal",
    "concentration_from_weights",
]
