"""Advisor context (bounded): autonomous paper-trade decision loop.

Owns the quant-in-the-loop paper-trading cycle: Monte-Carlo forward
distributions and skfolio weights (decision), hard risk ceilings (risk_gate),
the thin LLM trade chooser inside the gate-passing set (llm_decision), the
daily cycle orchestrator (cycle), champion/challenger strategy state
(strategy), self-critique (reflection), evolution rounds (evolution),
scorecards (scorecard), diagnostics, user-facing recommendations, and the
scheduler registrations (jobs).

This ``__init__`` is the context facade: it re-exports the callables other
contexts consume today. Import the package root, not internal submodules.
"""

from app.decision.advisor.cycle import ADVISOR_MANDATE, run_advisor_cycle
from app.decision.advisor.diagnostics import run_advisor_diagnostics
from app.decision.advisor.evolution import (
    EVOLUTION_RUN_NAME,
    composite_score,
    get_scorecard_history,
    next_resolution_at,
    run_evolution_round,
)
from app.decision.advisor.jobs import (
    register_advisor_cycle_job,
    register_evolution_job,
)
from app.decision.advisor.recommendations import compute_what_would_change
from app.decision.advisor.strategy import get_active_challenger, get_champion

__all__ = [
    "ADVISOR_MANDATE",
    "EVOLUTION_RUN_NAME",
    "composite_score",
    "compute_what_would_change",
    "get_active_challenger",
    "get_champion",
    "get_scorecard_history",
    "next_resolution_at",
    "register_advisor_cycle_job",
    "register_evolution_job",
    "run_advisor_cycle",
    "run_advisor_diagnostics",
    "run_evolution_round",
]
