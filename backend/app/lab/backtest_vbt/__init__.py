"""Vectorbt-based backtest engine for AlphaCrafter Trader (Phase 4)."""

from app.lab.backtest_vbt.engine import run_backtest
from app.lab.backtest_vbt.metrics import BacktestMetrics, compute_metrics
from app.lab.backtest_vbt.signals import (
    FittedParams,
    SmaCrossoverSignalGenerator,
)
from app.lab.backtest_vbt.walk_forward import walk_forward_test

__all__ = [
    "run_backtest",
    "walk_forward_test",
    "SmaCrossoverSignalGenerator",
    "FittedParams",
    "compute_metrics",
    "BacktestMetrics",
]
