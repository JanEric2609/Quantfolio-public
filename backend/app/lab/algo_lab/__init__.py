"""Algo lab: research-only maths for the algorithmic-trading section (ADR 0029, Proposed).

Pure functions and in-memory ledgers. No database, no broker, no order path, no
scheduler job, no API. Nothing here gates money; it measures how long a verdict
would take and whether a candidate's edge could cover its costs.
"""
from app.lab.algo_lab.eprocess import (
    BET_GRID,
    LedgerError,
    PromotionLedger,
    clipped_score,
)
from app.lab.algo_lab.gate_math import (
    breakeven_cost_bps,
    holm_alpha,
    minimum_detectable_sharpe,
    net_sharpe,
    oos_replication_ratio,
    per_order_cost_eur,
    prob_drawdown_breach,
    years_to_detect,
)

__all__ = [
    "BET_GRID",
    "LedgerError",
    "PromotionLedger",
    "breakeven_cost_bps",
    "clipped_score",
    "holm_alpha",
    "minimum_detectable_sharpe",
    "net_sharpe",
    "oos_replication_ratio",
    "per_order_cost_eur",
    "prob_drawdown_breach",
    "years_to_detect",
]
