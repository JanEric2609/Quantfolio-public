"""Graduation context (bounded): paper-to-real promotion gate.

Decides when the LLM has *statistically proven* skill on the paper portfolio
and may therefore surface recommendations for the user's real portfolio.

The gate is deliberately conservative. It combines Bailey & López de Prado's
Deflated Sharpe Ratio / Minimum Track Record Length (statistical-skill test,
deflated for the number of competing strategies) with practitioner-style
paper->live promotion criteria: sustained out-of-sample consistency, a
drawdown ceiling, a minimum decision sample, and learning maturity from the
competition self-critique loop.

Nothing here executes trades. A "graduated" verdict only unlocks *advisory*
recommendations that the user acts on manually, consistent with the project's
read-only safety constraints.

This ``__init__`` is the context facade: it re-exports the callables other
contexts consume today. Import the package root, not internal submodules.
"""
from app.decision.graduation.evaluator import (
    GraduationConfig,
    GraduationCriterion,
    GraduationResult,
    evaluate_graduation,
)
from app.decision.graduation.state import apply_graduation_state, is_graduated

__all__ = [
    "GraduationConfig",
    "GraduationCriterion",
    "GraduationResult",
    "apply_graduation_state",
    "evaluate_graduation",
    "is_graduated",
]
