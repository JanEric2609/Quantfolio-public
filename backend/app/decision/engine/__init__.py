"""Headless Portfolio Engine — Phase 1 of Dual Competition System.

Zero FastAPI / DB / SQLAlchemy dependencies.
Pure Python with Pydantic v2 models.
Deterministic, testable without infrastructure.

Components:
- state.py: PortfolioState + immutability
- executor.py: TCC TradeExecutor with deterministic gates
- performance.py: PerformanceTracker (reuses quant_metrics)
- rebalancer.py: Drift-based Rebalancer
- audit.py: Immutable AuditTrail
"""

from app.decision.engine.audit import AuditTrail
from app.decision.engine.executor import TradeExecutor
from app.decision.engine.performance import PerformanceTracker
from app.decision.engine.rebalancer import Rebalancer
from app.decision.engine.state import PortfolioState
from app.decision.engine.types import (
    Holding,
    Cash,
    PortfolioConfig,
    PortfolioStateSnapshot,
    TradeProposal,
    TradeAction,
    GateResult,
    ExecutedTrade,
    TradeBatch,
    PerformanceSnapshot,
    AuditEntry,
    EventType,
)

__all__ = [
    "Holding",
    "Cash",
    "PortfolioConfig",
    "TradeProposal",
    "TradeAction",
    "GateResult",
    "ExecutedTrade",
    "TradeBatch",
    "PerformanceSnapshot",
    "PortfolioStateSnapshot",
    "AuditEntry",
    "EventType",
    "PortfolioState",
    "TradeExecutor",
    "PerformanceTracker",
    "Rebalancer",
    "AuditTrail",
]
