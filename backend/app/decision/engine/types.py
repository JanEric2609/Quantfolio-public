"""Shared Pydantic types for the headless portfolio engine.

Constraint: Zero FastAPI / DB / SQLAlchemy dependencies.
Pure Python with Pydantic v2 — deterministic, testable without infrastructure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Core portfolio types
# ---------------------------------------------------------------------------


class Holding(BaseModel):
    """A single position in the portfolio."""

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(description="Unique asset identifier (ISIN or composite key)")
    symbol: str = Field(description="Display ticker/symbol")
    quantity: float = Field(ge=0, description="Number of units held")
    avg_price: float = Field(ge=0, description="Average entry price")
    currency: str = Field(default="EUR", min_length=3, max_length=3)


class Cash(BaseModel):
    """Cash balance in a single currency."""

    model_config = ConfigDict(frozen=True)

    currency: str = Field(default="EUR", min_length=3, max_length=3)
    amount: float = Field(description="Available cash (may be negative during multi-currency settlement)")


class PortfolioConfig(BaseModel):
    """Portfolio constraints and configuration."""

    model_config = ConfigDict(frozen=True)

    max_position_pct: float = Field(default=0.25, ge=0, le=1, description="Max single position as fraction of NAV")
    max_sector_pct: float = Field(default=0.40, ge=0, le=1, description="Max sector exposure as fraction of NAV")
    ucits_only: bool = Field(default=True, description="Restrict to UCITS-compliant assets")
    min_trade_value: float = Field(default=500.0, ge=0, description="Minimum notional trade size")
    rebalance_threshold_pct: float = Field(default=0.05, ge=0, le=0.5, description="Drift threshold for rebalancing")


# ---------------------------------------------------------------------------
# Trade execution types (TCC pattern)
# ---------------------------------------------------------------------------

TradeAction = Literal["buy", "sell"]


class TradeProposal(BaseModel):
    """A proposed trade from an agent, before gate checks."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    symbol: str = Field(min_length=1, description="Asset symbol/ticker")
    action: TradeAction
    quantity: float = Field(gt=0, description="Number of units to trade")
    currency: str = Field(default="EUR", min_length=3, max_length=3, description="Trade currency")
    limit_price: float | None = Field(default=None, description="Max buy price / min sell price")
    rationale: str = Field(default="", description="Why this trade is proposed")
    agent_source: str = Field(description="Which agent proposed this trade")
    confidence: float = Field(default=0.5, ge=0, le=1, description="Agent confidence 0-1")
    idempotency_key: str = Field(description="Unique key for deduplication: {run_id}:{seq}")

    @model_validator(mode="before")
    @classmethod
    def _default_symbol(cls, data: Any) -> Any:
        if isinstance(data, dict) and "symbol" not in data:
            data["symbol"] = data.get("asset_id", "")
        return data


class GateResult(BaseModel):
    """Result of a single pre-trade gate check."""

    model_config = ConfigDict(frozen=True)

    gate: str = Field(description="Gate name (cash_sufficiency, ucits, tradeability, concentration)")
    passed: bool
    reason: str | None = Field(default=None, description="Human-readable reason if failed")


class ExecutedTrade(BaseModel):
    """A trade that passed all gates and was executed."""

    model_config = ConfigDict(frozen=True)

    proposal: TradeProposal
    fill_price: float = Field(ge=0, description="Executed price")
    fill_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gate_results: list[GateResult] = Field(default_factory=list)
    idempotency_key: str


class TradeBatch(BaseModel):
    """A batch of trades executed atomically (all-or-nothing)."""

    model_config = ConfigDict(frozen=True)

    trades: list[ExecutedTrade] = Field(default_factory=list)
    state_before_snapshot: dict = Field(default_factory=dict, description="PortfolioState snapshot before execution")
    state_after_snapshot: dict = Field(default_factory=dict, description="PortfolioState snapshot after execution")
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# State snapshot type
# ---------------------------------------------------------------------------


class PortfolioStateSnapshot(BaseModel):
    """Point-in-time frozen snapshot of a complete portfolio state.

    Produced by PortfolioState.snapshot() — used for audit logging,
    before/after comparisons, and rollback recovery.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime = Field(description="UTC instant the snapshot was taken")
    holdings: dict[str, Holding] = Field(description="asset_id → Holding at snapshot time")
    cash: dict[str, Cash] = Field(description="currency → Cash at snapshot time")
    config: PortfolioConfig = Field(description="PortfolioConfig active at snapshot time")
    nav_total: float = Field(ge=0, description="Total portfolio NAV in base currency at snapshot time")


# ---------------------------------------------------------------------------
# Performance types
# ---------------------------------------------------------------------------


class PerformanceSnapshot(BaseModel):
    """Point-in-time performance metrics snapshot."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    total_value: dict[str, float] = Field(description="currency → total portfolio value")
    returns: dict[str, float] = Field(default_factory=dict, description="period → return (1D, 1W, 1M, 3M, 6M, 1Y)")
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Key metrics: sharpe, sortino, calmar, max_drawdown, volatility, cagr, alpha, beta",
    )


# ---------------------------------------------------------------------------
# Audit types
# ---------------------------------------------------------------------------

EventType = Literal[
    "trade_proposed",
    "gate_check",
    "trade_executed",
    "trade_rejected",
    "trade_cancelled",
    "rebalance_triggered",
    "state_snapshot",
    "performance_computed",
    "decision_logged",
]


class AuditEntry(BaseModel):
    """Immutable audit trail entry. Append-only — never modified after creation."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: EventType
    portfolio_id: str = Field(description="Which portfolio this event belongs to")
    run_id: str | None = Field(default=None, description="Competition run ID if applicable")
    details: dict = Field(default_factory=dict, description="Full event payload")
    idempotency_key: str | None = Field(default=None, description="Links to originating trade/proposal")
    sequence: int = Field(description="Monotonically increasing sequence number per portfolio")
