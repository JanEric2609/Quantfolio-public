"""Pydantic models for the Investment Recommendation Engine.

Defines the data contracts between context builder, LLM prompts,
validator, orchestrator, and API layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------

class Evidence(BaseModel):
    """A single evidence point backing a recommendation claim."""

    model_config = ConfigDict(from_attributes=True)

    source: str = Field(
        ...,
        description="Dot-notation path to the data source, e.g. 'metrics.VWCE.DE.sortino'",
    )
    value: Any = Field(
        default=None,
        description="The numeric or categorical value from that source",
    )
    interpretation: str = Field(
        default="",
        description="Human-readable explanation of what this value means",
    )


class SourceHealth(BaseModel):
    """Tracks which data services were available during context gathering."""

    model_config = ConfigDict(from_attributes=True)

    available: list[str] = Field(
        default_factory=list,
        description="Services that returned data successfully",
    )
    failed: list[str] = Field(
        default_factory=list,
        description="Services that raised exceptions",
    )
    degraded: list[str] = Field(
        default_factory=list,
        description="Services that returned partial or incomplete data",
    )


class DataHealth(BaseModel):
    """Overall data health for the recommendation run."""

    model_config = ConfigDict(from_attributes=True)

    available: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)
    completeness_score: float = Field(
        default=0.0,
        description="Fraction of requested data sources that returned complete data (0.0–1.0)",
    )


# ---------------------------------------------------------------------------
# Per-ticker recommendation
# ---------------------------------------------------------------------------

class RecommendationItem(BaseModel):
    """A single ticker recommendation with supporting evidence."""

    model_config = ConfigDict(from_attributes=True)

    ticker: str = Field(..., description="Ticker symbol, e.g. 'VWCE.DE'")
    action: str = Field(
        ...,
        description="Recommended action: BUY, HOLD, SELL, or AVOID",
    )
    confidence: str = Field(
        default="medium",
        description="Confidence level: low, medium, or high",
    )
    timeframe: str = Field(
        default="3-6 months",
        description="Investment horizon for this recommendation",
    )
    thesis: str = Field(
        default="",
        description="One-sentence investment thesis",
    )
    bull_case: str = Field(default="", description="Case for the position, if any")
    bear_case: str = Field(default="", description="Case against the position, if any")
    horizon_months: int | None = Field(
        default=None,
        description="Investment horizon in months, if the model provided one",
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="Structured evidence backing this recommendation",
    )
    risks: list[str] = Field(
        default_factory=list,
        description="Key risks for this position",
    )
    data_quality: str = Field(
        default="complete",
        description="Data quality: complete, partial, or insufficient",
    )


# ---------------------------------------------------------------------------
# Full recommendation report
# ---------------------------------------------------------------------------

class RecommendationReport(BaseModel):
    """Full recommendation report with all ticker recommendations.

    This model is both returned via API and persisted to the database.
    """

    model_config = ConfigDict(from_attributes=True)

    # Metadata
    report_id: str = Field(default="", description="Unique report identifier")
    generated_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the report was generated",
    )
    user_id: str = Field(default="", description="User who requested the report")

    # Safety flags (must always be present)
    estimate: bool = Field(
        default=True,
        description="Always True — these are estimates, not financial advice",
    )
    not_tax_advice: bool = Field(
        default=True,
        description="Always True — tax decisions require a qualified advisor",
    )

    # Portfolio context
    portfolio_value: float = Field(
        default=0.0,
        description="Total portfolio value at time of report",
    )
    currency: str = Field(default="EUR", description="Portfolio currency")

    # Regime context
    regime_label: str = Field(
        default="unknown",
        description="Current macro regime: bull, bear, high_vol, unknown",
    )
    regime_confidence: float = Field(
        default=0.0,
        description="Regime classification confidence (0.0–1.0)",
    )

    # Recommendations
    recommendations: list[RecommendationItem] = Field(
        default_factory=list,
        description="Per-ticker recommendations",
    )

    # Data health
    source_health: DataHealth = Field(
        default_factory=DataHealth,
        description="Which data sources were available/failed/degraded",
    )

    # Weights used for this report
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "quant_metrics": 0.25,
            "fundamentals": 0.25,
            "sentiment": 0.15,
            "regime": 0.20,
            "news": 0.15,
        },
        description="Category weights used for this report",
    )

    # Audit trail
    raw_llm_output: str = Field(
        default="",
        description="Full LLM response for debugging/audit",
    )
    validated_output: str = Field(
        default="",
        description="Cleaned JSON after source validation",
    )
    stripped_claims: list[str] = Field(
        default_factory=list,
        description="Claims removed during validation (unverifiable sources)",
    )


# ---------------------------------------------------------------------------
# Context bundle (passed to LLM)
# ---------------------------------------------------------------------------

class TickerMetrics(BaseModel):
    """Quant metrics for a single ticker."""

    model_config = ConfigDict(from_attributes=True)

    sortino: float | None = None
    sharpe: float | None = None
    calmar: float | None = None
    cvar_95: float | None = None
    beta: float | None = None
    alpha: float | None = None
    max_drawdown: float | None = None
    annualised_return: float | None = None
    annualised_volatility: float | None = None


class TickerFundamentals(BaseModel):
    """Fundamental data for a single ticker."""

    model_config = ConfigDict(from_attributes=True)

    piotroski_score: int | None = None
    pe_ratio: float | None = None
    roe: float | None = None
    debt_equity: float | None = None
    profit_margin: float | None = None
    revenue_growth: float | None = None
    gross_margin: float | None = None


class TickerEstimates(BaseModel):
    """Forward-earnings analyst-estimate signal for a single ticker, from
    IBES consensus snapshots. Ticker-string matched, not gvkey-verified (see
    ``app.foundation.data_engineering.pit_panel_joins`` module docstring) --
    ``data_confidence`` lets thesis text and the UI flag that caveat."""

    model_config = ConfigDict(from_attributes=True)

    sue: float | None = Field(
        default=None,
        description="Standardized unexpected earnings: (actual - meanest) / stdev, only populated once actual is announced",
    )
    revision_momentum: float | None = Field(
        default=None,
        description="Fractional change in consensus mean estimate vs. the prior snapshot; positive means analysts raised estimates",
    )
    dispersion: float | None = Field(
        default=None,
        description="Forecast-uncertainty flag: stdev / abs(meanest)",
    )
    data_confidence: str | None = Field(
        default=None,
        description="'ticker_match' (best-effort string match, no verified crosswalk) or None if unavailable",
    )


class TickerInsiderSignal(BaseModel):
    """SEC Form 4 insider-trading signal for a single ticker. Ticker-string
    matched, not gvkey-verified (see
    ``app.foundation.data_engineering.pit_panel_joins`` module docstring) --
    routine (10b5-1-style) traders are already excluded upstream."""

    model_config = ConfigDict(from_attributes=True)

    cluster_buy_score: int | None = Field(
        default=None,
        description="Distinct opportunistic insiders making open-market purchases in the trailing 30 days",
    )
    net_insider_flow_usd: float | None = Field(
        default=None,
        description="Dollar-weighted opportunistic buys minus sells in the trailing 90 days",
    )
    data_confidence: str | None = Field(
        default=None,
        description="'ticker_match' (best-effort string match, no verified crosswalk) or None if unavailable",
    )


class TickerSentiment(BaseModel):
    """Sentiment data for a single ticker."""

    model_config = ConfigDict(from_attributes=True)

    finbert_score: float | None = Field(
        default=None,
        description="FinBERT sentiment score (-1.0 to 1.0)",
    )
    news_count: int = Field(default=0, description="Number of recent news articles")
    headlines: list[str] = Field(
        default_factory=list,
        description="Top recent headlines",
    )


class PortfolioContext(BaseModel):
    """Portfolio-level context."""

    model_config = ConfigDict(from_attributes=True)

    holdings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of holding dicts with ticker, weight, value, etc.",
    )
    total_value: float = Field(default=0.0)
    currency: str = Field(default="EUR")
    asset_allocation: dict[str, float] = Field(
        default_factory=dict,
        description="Asset class allocation percentages",
    )


class RegimeContext(BaseModel):
    """Macro regime context."""

    model_config = ConfigDict(from_attributes=True)

    label: str = Field(default="unknown")
    confidence: float = Field(default=0.0)
    vix: float | None = None
    macro_snapshot: dict[str, Any] = Field(default_factory=dict)


class TickerTrackRecord(BaseModel):
    """Historical decision track record for a single ticker, drawn from both
    the advisor loop (portfolio-level composite score — advisor's scorecard
    is per-sleeve, not per-ticker, so this figure is the same across every
    ticker in one report) and the llm_portfolio mandate reviews (genuinely
    per-ticker verdict history)."""

    model_config = ConfigDict(from_attributes=True)

    advisor_composite_score: float | None = Field(
        default=None,
        description="Advisor sleeve's collapsed 0-1 4-axis composite score (portfolio-level, not ticker-specific)",
    )
    advisor_n_resolved: int = Field(default=0, description="Resolved predictions behind the advisor composite")
    llm_verdict_history: list[str] = Field(
        default_factory=list,
        description="This ticker's past mandate-review verdicts (hit/miss/partial), most recent first",
    )
    llm_win_rate: float | None = Field(
        default=None,
        description="Fraction of this ticker's llm_portfolio verdicts that were 'hit' (None if none scored yet)",
    )


class UserProfile(BaseModel):
    """User investment profile."""

    model_config = ConfigDict(from_attributes=True)

    risk_tolerance: str = Field(default="moderate")
    investment_horizon: str = Field(default="long")
    current_holdings_only: bool = Field(
        default=True,
        description="True if user only has ETFs, no individual stocks yet",
    )


class ContextBundle(BaseModel):
    """Complete context bundle passed to the LLM for recommendation generation.

    Each section is independently gathered — if a service fails, that section
    is None or contains partial data, and data_health tracks the status.
    """

    model_config = ConfigDict(from_attributes=True)

    portfolio: PortfolioContext = Field(default_factory=PortfolioContext)
    metrics: dict[str, TickerMetrics] = Field(
        default_factory=dict,
        description="Per-ticker quant metrics keyed by ticker symbol",
    )
    fundamentals: dict[str, TickerFundamentals] = Field(
        default_factory=dict,
        description="Per-ticker fundamental data keyed by ticker symbol",
    )
    estimates: dict[str, TickerEstimates] = Field(
        default_factory=dict,
        description="Per-ticker IBES forward-earnings estimate signal keyed by ticker symbol",
    )
    insider_signal: dict[str, TickerInsiderSignal] = Field(
        default_factory=dict,
        description="Per-ticker SEC insider-trading signal keyed by ticker symbol",
    )
    sentiment: dict[str, TickerSentiment] = Field(
        default_factory=dict,
        description="Per-ticker sentiment data keyed by ticker symbol",
    )
    track_record: dict[str, TickerTrackRecord] = Field(
        default_factory=dict,
        description="Per-ticker historical decision track record (advisor + llm_portfolio), keyed by ticker symbol",
    )
    regime: RegimeContext = Field(default_factory=RegimeContext)
    user_profile: UserProfile = Field(default_factory=UserProfile)
    data_health: DataHealth = Field(default_factory=DataHealth)
