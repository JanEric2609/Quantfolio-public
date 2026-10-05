"""Factor premia on the JKP panel (report Phase 3): the evidence behind the tilt.

Builds value, momentum, profitability, investment and value+momentum
portfolios from the JKP characteristics panel and grades each against a
pre-registered, prior-informed test. See ``CONTEXT.md``.
"""

from app.lab.factor_premia.evidence import EvidenceCard, evaluate
from app.lab.factor_premia.portfolios import factor_series
from app.lab.factor_premia.strategies import REGIONS, STRATEGIES, STRATEGY_BY_KEY, Strategy
from app.lab.factor_premia.study import TRIAL_CONTEXT, latest_cards, run_study

__all__ = [
    "REGIONS",
    "STRATEGIES",
    "STRATEGY_BY_KEY",
    "TRIAL_CONTEXT",
    "EvidenceCard",
    "Strategy",
    "evaluate",
    "factor_series",
    "latest_cards",
    "run_study",
]
