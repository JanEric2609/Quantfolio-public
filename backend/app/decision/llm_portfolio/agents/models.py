"""Pydantic models for agent inputs, outputs, and configuration.

Phase 2 — Multi-Agent Council (Dual Competition System).

All agents produce structured outputs via Pydantic validation.
LLM is used for judgment only — arithmetic stays in deterministic Python.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.decision.engine.types import TradeAction


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Configuration for a single council agent.

    All agents share the same LLM endpoint but differ in system prompt,
    temperature, and tool access. Reasoning is ALWAYS disabled per
    Machine Spirits mitigation constraints (Yang 2026).
    """

    model_config = ConfigDict(frozen=True)

    portfolio_id: str = Field(description="Which competition portfolio this agent serves")
    strategy_prompt: str = Field(description="Agent personality + strategy instructions")
    llm_endpoint: str = Field(default="", description="llama.cpp base URL")
    llm_model: str = Field(default="qwen3.5-9b", description="Model identifier sent to /chat/completions")
    # Deprecated for payloads: council calls send the Qwen non-thinking preset
    # outright (plan D4); retained for config compatibility only.
    temperature: float = Field(default=0.3, ge=0, le=2, description="LLM sampling temperature")
    max_tokens: int = Field(default=4096, ge=1, description="Max output tokens per LLM call")
    reasoning_enabled: Literal[False] = Field(default=False, description="MUST be False (Machine Spirits mitigation)")
    context_window: int = Field(default=28000, ge=1, description="Max prompt characters")


# ---------------------------------------------------------------------------
# Analyst agent
# ---------------------------------------------------------------------------


class Opportunity(BaseModel):
    """A single investment opportunity identified by the AnalystAgent."""

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(description="Asset identifier (ISIN or symbol)")
    symbol: str = Field(description="Display ticker")
    action: TradeAction = Field(description="Buy or sell recommendation")
    score: float = Field(default=50.0, ge=0, le=100, description="Attractiveness score 0-100")
    rationale: str = Field(description="Why this opportunity is identified")


class RiskFlag(BaseModel):
    """A risk concern flagged by the AnalystAgent."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    symbol: str
    risk_type: Literal["concentration", "volatility", "drawdown", "correlation", "liquidity", "sentiment"]
    severity: float = Field(default=0.5, ge=0, le=1, description="0 = benign, 1 = critical")
    description: str = Field(description="Human-readable risk description")


class AnalysisReport(BaseModel):
    """Output from AnalystAgent — opportunities + risk flags + narrative."""

    model_config = ConfigDict(frozen=True)

    portfolio_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    opportunities: list[Opportunity] = Field(default_factory=list, description="Ranked investment opportunities")
    risk_flags: list[RiskFlag] = Field(default_factory=list, description="Flagged concerns")
    market_narrative: str = Field(default="", description="LLM-written market narrative summary")
    confidence: float = Field(default=0.5, ge=0, le=1, description="Overall analyst confidence")


# ---------------------------------------------------------------------------
# Risk agent
# ---------------------------------------------------------------------------


class TradeRisk(BaseModel):
    """Per-trade risk assessment computed by RiskAgent."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(description="Matches TradeProposal.idempotency_key")
    var_impact: float = Field(ge=0, description="Incremental VaR contribution (loss magnitude)")
    cvar_impact: float = Field(ge=0, description="Incremental CVaR contribution")
    concentration_risk: float = Field(default=0, ge=0, le=1, description="Post-trade concentration level 0-1")
    correlation_warning: str | None = Field(default=None, description="Warning if trade increases correlation")
    risk_score: float = Field(default=0.5, ge=0, le=1, description="Aggregate risk score 0=low risk, 1=high risk")


class RiskReport(BaseModel):
    """Output from RiskAgent — portfolio-level risk assessment."""

    model_config = ConfigDict(frozen=True)

    portfolio_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    var_95: float = Field(ge=0, description="1-day 95% VaR (loss magnitude)")
    cvar_95: float = Field(ge=0, description="1-day 95% CVaR")
    max_drawdown: float = Field(ge=0, description="Current max drawdown")
    concentration_warnings: list[str] = Field(default_factory=list, description="Excess concentration per position/sector")
    correlation_warnings: list[str] = Field(default_factory=list, description="High correlation pairs")
    per_trade_risk: list[TradeRisk] = Field(default_factory=list)
    risk_budget_remaining: float = Field(default=1.0, ge=0, le=1, description="Fraction of risk budget remaining")
    narrative: str = Field(default="", description="LLM-written risk assessment summary")


# ---------------------------------------------------------------------------
# Macro agent
# ---------------------------------------------------------------------------


RegimeLabel = Literal["expansion", "contraction", "crisis", "unknown"]
RiskStance = Literal["risk_on", "risk_off", "neutral"]


class SectorAllocation(BaseModel):
    """Per-sector weight recommended by MacroAgent."""

    model_config = ConfigDict(frozen=True)

    sector: str = Field(description="Sector name (e.g., Technology, Energy)")
    weight: float = Field(default=0, ge=0, le=1, description="Recommended allocation fraction")
    rationale: str = Field(default="")


class MacroReport(BaseModel):
    """Output from MacroAgent — regime detection + sector guidance."""

    model_config = ConfigDict(frozen=True)

    portfolio_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    regime: RegimeLabel = Field(default="unknown", description="Current market regime")
    risk_stance: RiskStance = Field(default="neutral", description="Risk-on / risk-off / neutral")
    regime_confidence: float = Field(default=0.5, ge=0, le=1, description="Confidence in regime classification")
    sector_weights: list[SectorAllocation] = Field(default_factory=list, description="Sector rotation guidance")
    macro_narrative: str = Field(default="", description="LLM-written macro narrative")
    risk_budget_adjustment: float = Field(default=0, ge=-0.5, le=0.5, description="Adjust risk budget ±50% based on regime")


# ---------------------------------------------------------------------------
# Portfolio Manager agent
# ---------------------------------------------------------------------------


class RiskBudgetItem(BaseModel):
    """Per-sector/per-theme risk budget allocation."""

    model_config = ConfigDict(frozen=True)

    category: str = Field(description="Sector or strategy theme")
    allocation: float = Field(default=0, ge=0, le=1, description="Risk budget fraction for this category")


class TradeProposalItem(BaseModel):
    """A single trade proposal inside a DecisionReport."""

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(description="Asset identifier (ISIN or symbol)")
    action: Literal["buy", "sell"] = Field(description="Trade direction")
    quantity: float = Field(description="Number of units/shares to trade")
    price: float = Field(default=0.0, description="Estimated execution price per unit/share")
    rationale: str = Field(default="", description="Why this trade is proposed")
    idempotency_key: str = Field(default="", description="Unique key for idempotent execution")


class DecisionReport(BaseModel):
    """Output from PortfolioManagerAgent — final trade decisions + BLM views."""

    model_config = ConfigDict(frozen=True)

    portfolio_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trade_proposals: list[TradeProposalItem] = Field(
        default_factory=list,
        description="List of proposed trades",
    )
    target_weights: dict[str, float] = Field(default_factory=dict, description="asset_id → target weight")
    risk_budget_allocation: dict[str, float] = Field(
        default_factory=dict, description="category → risk budget fraction"
    )
    blm_views: dict[str, float] = Field(
        default_factory=dict,
        description="asset_id → expected return (LLM view for Black-Litterman)",
    )
    strategy_narrative: str = Field(default="", description="LLM-written strategy narrative")
    confidence: float = Field(default=0.5, ge=0, le=1, description="Overall decision confidence")


# ---------------------------------------------------------------------------
# Debate agent
# ---------------------------------------------------------------------------


class DebateReport(BaseModel):
    """Output from DebateAgent — adversarial review of both portfolios."""

    model_config = ConfigDict(frozen=True)

    portfolio_a_id: str
    portfolio_b_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    portfolio_a_critique: list[str] = Field(default_factory=list, description="Weaknesses found in portfolio A's decisions")
    portfolio_b_critique: list[str] = Field(default_factory=list, description="Weaknesses found in portfolio B's decisions")
    portfolio_a_confidence: float = Field(default=0.5, ge=0, le=1, description="Revised confidence in portfolio A")
    portfolio_b_confidence: float = Field(default=0.5, ge=0, le=1, description="Revised confidence in portfolio B")
    suggested_revisions: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="portfolio_id → suggested trade revisions",
    )
    narrative: str = Field(default="", description="LLM-written debate summary")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class CouncilResult(BaseModel):
    """Complete result of one portfolio's agent council run."""

    model_config = ConfigDict(frozen=True)

    portfolio_id: str
    analysis: AnalysisReport
    risk: RiskReport
    macro: MacroReport
    decision: DecisionReport
    errors: list[str] = Field(default_factory=list, description="Non-fatal errors encountered during council run")


class CompetitionCouncilResult(BaseModel):
    """Complete competition council result — both portfolios + debate."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    portfolio_a: CouncilResult
    portfolio_b: CouncilResult
    debate: DebateReport
    errors: list[str] = Field(default_factory=list)
