"""LLM portfolio context (bounded): mandate-driven LLM paper management.

Orchestrates paper portfolios that mirror real DKB holdings and run
mandate-driven LLM reviews with deterministic buy gates: mirror-portfolio
provisioning (mirror), review-context assembly (context), buy gates
(gates), the review runner (review), divergence advice cards (divergence),
the Multi-Agent Council (agents/) plus its context bridge (council_context),
scoring, assessment, and BLM views.

This ``__init__`` is the context facade: it re-exports the callables other
contexts consume today. Import the package root, not internal submodules —
in particular, ``agents/`` is no longer internal-only (it was until the
council was wired into ``review.run_mandate_review`` and
``advisor.evolution.run_evolution_round``); other contexts (namely
``advisor``) must still reach it only through this facade, never
``app.decision.llm_portfolio.agents`` directly.
"""
from __future__ import annotations

from app.decision.llm_portfolio.agents import AgentConfig, CompetitionOrchestrator, create_default_config
from app.decision.llm_portfolio.context import assemble_review_context
from app.decision.llm_portfolio.council_context import build_council_context
from app.decision.llm_portfolio.divergence import compute_divergence, generate_advice_cards
from app.decision.llm_portfolio.gates import GateResult, check_buy_gates
from app.decision.llm_portfolio.jobs import (
    register_llm_portfolio_review_jobs,
    register_llm_review_scoring_job,
)
from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio
from app.decision.llm_portfolio.review import run_mandate_review
from app.decision.llm_portfolio.scoring import get_decision_verdicts

__all__ = [
    "AgentConfig",
    "CompetitionOrchestrator",
    "GateResult",
    "assemble_review_context",
    "build_council_context",
    "check_buy_gates",
    "compute_divergence",
    "create_default_config",
    "ensure_mandate_portfolio",
    "generate_advice_cards",
    "get_decision_verdicts",
    "register_llm_portfolio_review_jobs",
    "register_llm_review_scoring_job",
    "run_mandate_review",
]
