"""Performance ledger services (Phase 3)."""

from app.lab.performance_ledger.composites import (
    create_composite,
    list_composites,
    ensure_default_composite,
    get_composite,
    add_portfolio_to_composite,
    remove_portfolio_from_composite,
    get_composite_members,
)
from app.lab.performance_ledger.twr import time_weighted_return, TWRPeriod
from app.lab.performance_ledger.composite_twr import composite_time_weighted_return
from app.lab.performance_ledger.mwr import money_weighted_return, money_weighted_return_irr
from app.lab.performance_ledger.dispersion import asset_weighted_dispersion, DispersionMetrics
from app.lab.performance_ledger.ex_post_risk import (
    calculate_ex_post_risk,
    ex_post_risk_to_dict,
    ExPostRiskMetrics,
)
from app.lab.performance_ledger.ledger_writer import write_ledger_snapshot

__all__ = [
    "create_composite",
    "list_composites",
    "ensure_default_composite",
    "get_composite",
    "add_portfolio_to_composite",
    "remove_portfolio_from_composite",
    "get_composite_members",
    "time_weighted_return",
    "TWRPeriod",
    "composite_time_weighted_return",
    "money_weighted_return",
    "money_weighted_return_irr",
    "asset_weighted_dispersion",
    "DispersionMetrics",
    "calculate_ex_post_risk",
    "ex_post_risk_to_dict",
    "ExPostRiskMetrics",
    "write_ledger_snapshot",
]
