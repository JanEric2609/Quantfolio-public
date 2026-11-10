"""Graduation context (bounded): the capital ladder (ADR 0025).

Decides how much of the real book the satellite (single stocks) may hold,
given the evidence of a candidate's paper record against MSCI World: paper
0 %, Tuition (a small budgeted weight), Earned 10 % (skill e-value >= 10 e^2),
Proven 15-20 % (>= 40 e^2). Harm evidence or a drawdown beyond its band sends
a candidate back to paper as a new epoch. It caps the cost of an unproven
idea; it is not a skill detector (``ladder.py``).

The old multi-criterion gate (``evaluator.py``) stays for display, marked
legacy; its Deflated Sharpe / PBO checks moved to the lab
(``app.lab.evidence_gate.admission``).

Nothing here executes trades. The ladder only sets the satellite target of
the monthly plan (advice), consistent with the project's read-only safety
constraints.

This ``__init__`` is the context facade: it re-exports the callables other
contexts consume today. Import the package root, not internal submodules.
"""
from app.decision.graduation.evaluator import (
    GraduationConfig,
    GraduationCriterion,
    GraduationResult,
    evaluate_graduation,
)
from app.decision.graduation.ladder import (
    ladder_funding,
    ladder_state,
    ladder_weight,
    run_ladder,
)
from app.decision.graduation.state import apply_graduation_state, is_graduated

__all__ = [
    "GraduationConfig",
    "GraduationCriterion",
    "GraduationResult",
    "apply_graduation_state",
    "evaluate_graduation",
    "is_graduated",
    "ladder_funding",
    "ladder_state",
    "ladder_weight",
    "run_ladder",
]
