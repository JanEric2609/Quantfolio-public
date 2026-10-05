"""Custom backtest engine + cost model + first signal batch (ADR 0015 Phase 4, "data spine" -> "lab").

See CONTEXT.md for the full picture.
"""
from app.lab.quant_lab.costs import CostModel, TaxDragResult, apply_trade_costs, estimate_tax_drag
from app.lab.quant_lab.cv import LabFold, purged_embargo_splits
from app.lab.quant_lab.engine import LabBacktestResult, LabBacktestSpec, run_lab_backtest
from app.lab.quant_lab.strategy_backtest import STRATEGIES, run_strategy_backtest, strategy_catalog
from app.lab.quant_lab.signal_batch import (
    FactorResult,
    PricePanel,
    SignalBatchResult,
    build_price_panel,
    compute_microstructure_factors,
    run_signal_batch,
)
from app.lab.quant_lab.universe import LAB_UNIVERSE

__all__ = [
    "CostModel",
    "TaxDragResult",
    "apply_trade_costs",
    "estimate_tax_drag",
    "LabFold",
    "purged_embargo_splits",
    "LabBacktestResult",
    "LabBacktestSpec",
    "run_lab_backtest",
    "STRATEGIES",
    "run_strategy_backtest",
    "strategy_catalog",
    "FactorResult",
    "PricePanel",
    "SignalBatchResult",
    "build_price_panel",
    "compute_microstructure_factors",
    "run_signal_batch",
    "LAB_UNIVERSE",
]
