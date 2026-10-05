from datetime import date as DateType, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# `DateType` aliases datetime.date so that Pydantic fields *named* `date` can be
# annotated without the field name shadowing the type in the class namespace
# (e.g. `date: Optional[DateType] = Field(default=None)`); `date` remains the type
# for non-colliding fields such as `buy_date`/`start_date`. Do not collapse this —
# using `date: Optional[date] = Field(...)` raises PydanticSchemaGenerationError.
date = DateType


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    message: str


class AuthSession(BaseModel):
    user_id: str
    username: str
    generate_summary: bool = False


class PasskeyOptionsRequest(BaseModel):
    username: str | None = None
    name: str = "Passkey"


class PasskeyRegisterVerifyRequest(BaseModel):
    credential_id: str | None = None
    public_key: str = ""
    challenge: str
    sign_count: int = 0
    name: str = "Passkey"
    transports: list[str] = Field(default_factory=list)
    credential: dict[str, Any] | None = None


class PasskeyAuthenticateVerifyRequest(BaseModel):
    credential_id: str
    challenge: str
    sign_count: int = 0
    credential: dict[str, Any] | None = None


class TargetAllocationItem(BaseModel):
    """Validated target allocation entry for /api/portfolio/target-allocation PUT.

    ``asset_type`` here is a goal-planning allocation bucket, not an instrument
    classification — deliberately a different, broader taxonomy than ``AssetType``
    below (see ``app.foundation.instrument_taxonomy`` for the instrument-level one).
    """

    asset_type: Literal["equities", "bonds", "cash", "crypto", "real_estate", "commodities"]
    target_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    tolerance_pct: float = Field(default=5.0, ge=0.0, le=20.0)


class PasskeyCredentialOut(OrmModel):
    id: str
    credential_id: str
    name: str
    created_at: datetime
    last_used: datetime | None = None


class TokenResponse(BaseModel):
    session: AuthSession


class SetupStatus(BaseModel):
    initialized: bool
    has_passkey: bool
    integrations: dict[str, bool]
    next_step: str


class IntegrationTestRequest(BaseModel):
    service: Literal["llm", "dkb", "scalable", "alphavantage", "finnhub", "openbb", "obsidian", "smtp", "fred", "eod", "anthropic", "openai", "rss", "massive", "telegram", "twelvedata", "databento", "tiingo", "alpaca", "ecb_sdw", "yfinance", "justetf"]
    value: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class IntegrationTestResponse(BaseModel):
    service: str
    ok: bool
    message: str
    # Set when the result was recorded (a test of the saved configuration).
    tested_at: str | None = None


class SettingsPayload(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    integrations: dict[str, dict[str, Any]] = Field(default_factory=dict)


class HoldingIn(BaseModel):
    isin: str | None = None
    ticker: str | None = None
    name: str
    asset_type: str = "stock"
    quantity: Decimal
    avg_buy_price: Decimal
    buy_date: date | None = None
    currency: str = "EUR"
    source: str = "manual"


class HoldingOut(HoldingIn, OrmModel):
    id: str
    portfolio_id: str
    avg_buy_price: Decimal | None = None
    dkb_available: bool = False


class ExpenseIn(BaseModel):
    date: date
    amount: Decimal
    currency: str = "EUR"
    description: str
    category_id: str | None = None
    source: str = "manual"
    notes: str | None = None


class ExpenseOut(ExpenseIn, OrmModel):
    id: str
    account_name: str | None = None


class DkbSyncResponse(BaseModel):
    session_id: str
    state: Literal[
        "pending_tan",
        "waiting_for_push",
        "needs_manual_tan",
        "confirmed",
        "failed",
        "cached",
        "expired",
    ]
    message: str
    challenge: str | None = None
    challenge_html: str | None = None
    decoupled: bool = False
    available_tan_methods: list[dict[str, str]] = Field(default_factory=list)
    next_poll_after_seconds: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    provider: str = "fints"
    logs: list[dict[str, Any]] = Field(default_factory=list)


class DkbProviderStatus(BaseModel):
    provider: str
    configured: bool
    product_id_required: bool
    product_id_configured: bool
    product_id_preview: str | None = None
    product_id_source: str | None = None
    message: str


class NewsOut(OrmModel):
    id: str
    title: str
    summary: str | None = None
    url: str
    source: str
    ticker: str | None = None
    sentiment_score: Decimal | None = None
    sentiment_label: str | None = None
    published_at: datetime
    is_macro: bool


class RecommendationPayload(BaseModel):
    ticker: str | None = None
    horizon: Literal["short", "mid", "long"] = "mid"
    verdict: Literal["BUY", "HOLD", "SELL", "AVOID", "WATCH"]
    confidence: float = Field(ge=0, le=100)
    thesis: str = ""
    reasoning: str = ""
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    portfolio_fit: str
    dkb_available: bool = False
    risk_notes: list[str] = Field(default_factory=list)
    sell_trigger: str | None = None
    sell_reasoning: str | None = None
    currency_note: str | None = None
    backtest_summary: dict[str, Any] = Field(default_factory=dict)
    macro_context: str | None = None
    generated_at: datetime | None = None
    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Structured evidence backing this recommendation: "
            "{source: dot-path into the analysis context, value, interpretation}. "
            "Stripped of any source that doesn't resolve against the context before persisting."
        ),
    )

    def model_post_init(self, __context: Any) -> None:
        if self.verdict == "SELL" and not self.sell_reasoning:
            raise ValueError("SELL recommendations require sell_reasoning")
        if not self.thesis and self.reasoning:
            self.thesis = self.reasoning[:500]
        if not self.reasoning and self.thesis:
            self.reasoning = self.thesis


AssetType = Literal["stock", "etf", "bond", "bond_etf", "fund", "money_market", "cash", "other"]
RecommendationMode = Literal["long_term", "short_term"]
ApprovalState = Literal[
    "draft",
    "needs_review",
    "approved_watchlist",
    "approved_candidate",
    "rejected",
    "expired",
    "acted_on_manually",
]


class ProviderQuality(BaseModel):
    stale: bool = False
    as_of: datetime | None = None
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


class ProviderResult(BaseModel):
    ok: bool
    provider: str
    data: Any = None
    quality: ProviderQuality = Field(default_factory=ProviderQuality)
    error: str | None = None


class RecommendationAsset(BaseModel):
    symbol: str | None = None
    isin: str | None = None
    name: str
    asset_type: AssetType = "stock"
    currency: str = "EUR"


class SuggestedPositionSize(BaseModel):
    min_pct: float = Field(ge=0, le=100)
    max_pct: float = Field(ge=0, le=100)
    reason: str


class ScoreBreakdown(BaseModel):
    valuation: float | None = None
    quality: float | None = None
    profitability: float | None = None
    growth: float | None = None
    momentum: float | None = None
    volatility: float | None = None
    drawdown: float | None = None
    liquidity: float | None = None
    fundamental_strength: float | None = None
    etf_structure: float | None = None
    macro_fit: float | None = None
    portfolio_fit: float | None = None
    tax_efficiency: float | None = None


class BenchmarkComparison(BaseModel):
    primary_benchmark: str = "EUNL.DE"
    excess_return: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    relative_drawdown: float | None = None
    notes: str = ""


class BacktestSummaryV2(BaseModel):
    status: str = "unavailable"
    period: str = "latest available data"
    cagr: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    volatility: float | None = None
    hit_rate: float | None = None
    turnover: float | None = None
    transaction_cost_assumption: str = "10 bps per rebalance"
    slippage_assumption: str = "5 bps spread/slippage"
    warnings: list[str] = Field(default_factory=list)


class RecommendationPayloadV2(BaseModel):
    asset: RecommendationAsset
    mode: RecommendationMode
    horizon_months: int = Field(ge=1, le=120)
    verdict: Literal[
        "STRONG_CANDIDATE",
        "CANDIDATE",
        "WATCH",
        "HOLD_EXISTING",
        "AVOID",
        "REJECTED_BY_RISK",
        "STALE",
    ]
    approval_state: ApprovalState = "needs_review"
    confidence: float = Field(ge=0, le=100)
    evidence_score: float = Field(ge=0, le=100)
    data_quality_score: float = Field(ge=0, le=100)
    risk_score: float = Field(ge=0, le=100)
    portfolio_fit_score: float = Field(ge=0, le=100)
    expected_role: Literal["core", "satellite", "hedge", "cash_like", "income", "speculative"]
    suggested_position_size: SuggestedPositionSize
    score_breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    benchmark_comparison: BenchmarkComparison = Field(default_factory=BenchmarkComparison)
    backtest_summary: BacktestSummaryV2 = Field(default_factory=BacktestSummaryV2)
    thesis: str
    bear_case: str
    bull_case: str
    risk_notes: list[str] = Field(default_factory=list)
    why_not_buy: list[str] = Field(default_factory=list)
    invalidation_triggers: list[str] = Field(default_factory=list)
    required_human_checks: list[str] = Field(default_factory=list)
    source_reports: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime
    expires_at: datetime


class ObsidianQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


class ChatStreamRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class ChatConversationOut(BaseModel):
    conversation_id: str
    title: str
    last_at: str
    message_count: int


class ChatMessageOut(BaseModel):
    id: str
    conversation_id: str | None = None
    role: str
    content: str
    timestamp: str


class BacktestRequest(BaseModel):
    ticker: str
    strategy: str = "sma_cross"
    start: date | None = None
    end: date | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ReportCreateRequest(BaseModel):
    title: str = "TradingAgents report"
    ticker: str | None = None
    horizon: Literal["short", "mid", "long"] = "mid"
    content: str = Field(min_length=20, max_length=200_000)
    source: str = "manual"


class ReportOut(OrmModel):
    id: str
    title: str
    ticker: str | None = None
    horizon: str
    source: str
    content: str
    created_at: datetime


class RecommendationGenerateRequest(BaseModel):
    horizon: Literal["short", "mid", "long"] = "mid"
    mode: RecommendationMode | None = None
    limit: int = Field(default=5, ge=1, le=20)
    universe: Literal["etfs_blue_chips", "default_global", "etfs", "stocks", "portfolio", "watchlist"] = "etfs_blue_chips"


class RecommendationGenerateResult(BaseModel):
    created: list[dict[str, Any]] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""


class WatchlistIn(BaseModel):
    ticker: str | None = None
    isin: str | None = None
    name: str
    notes: str | None = None
    horizon_tag: Literal["short", "mid", "long"] = "mid"
    target_price: Decimal | None = None


class WatchlistOut(OrmModel):
    id: str
    user_id: str
    ticker: str | None = None
    isin: str | None = None
    name: str
    added_date: date
    notes: str | None = None
    horizon_tag: str
    target_price: Decimal | None = None
    alert_triggered: bool = False
    alert_triggered_at: datetime | None = None


class WatchlistAlertIn(BaseModel):
    target_price: Decimal | None = None


class SubscriptionIn(BaseModel):
    name: str
    amount: Decimal
    currency: str = "EUR"
    billing_cycle: Literal["monthly", "quarterly", "annual", "weekly"] = "monthly"
    next_due_date: date
    category_id: str | None = None
    payment_method: str | None = None
    logo_url: str | None = None
    active: bool = True
    notes: str | None = None


class IncomeSourceIn(BaseModel):
    name: str
    amount: Decimal
    currency: str = "EUR"
    cadence: Literal["weekly", "monthly", "quarterly", "annual"] = "monthly"
    next_date: date
    active: bool = True


class InvoiceIn(BaseModel):
    issuer: str
    amount: Decimal
    currency: str = "EUR"
    due_date: date | None = None
    paid: bool = False
    notes: str | None = None


class MarkPaidRequest(BaseModel):
    paid_date: date | None = None
    create_expense: bool = True


class CategoryIn(BaseModel):
    name: str
    color: str = "#3B82F6"
    icon: str = "tag"
    type: Literal["income", "expense", "investment"] = "expense"
    target_amount: Decimal | None = None
    target_date: date | None = None

    @model_validator(mode="after")
    def _check_goal_fields(self) -> "CategoryIn":
        if (self.target_amount is None) != (self.target_date is None):
            raise ValueError("target_amount and target_date must be both set or both omitted")
        return self


class MarketQuote(BaseModel):
    ticker: str
    price: float | None = None
    currency: str = "EUR"
    source: str
    stale: bool = False
    message: str | None = None


class MarketHistoryPoint(BaseModel):
    date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float
    volume: float | None = None
    source: str
    stale: bool = False


class MarketFundamentals(BaseModel):
    ticker: str
    data: dict[str, Any] = Field(default_factory=dict)
    source: str
    stale: bool = False
    message: str | None = None


class PriceEnsureRequest(BaseModel):
    ticker: str
    days: int = Field(default=365, ge=30, le=3650)


class QuantExperimentIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    mode: RecommendationMode = "long_term"
    universe: list[str] = Field(default_factory=lambda: ["EUNL.DE", "VWCE.DE"])
    benchmark_symbol: str | None = "EUNL.DE"
    start_date: date | None = None
    end_date: date | None = None
    rebalance_frequency: str = "monthly"
    strategy_type: str = "momentum"
    config: dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class GraveyardIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)
    failed_metrics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Optimisation
# ---------------------------------------------------------------------------

class OptimRunRequest(BaseModel):
    objective: Literal["min_risk", "max_return", "max_utility", "max_ratio"] = "max_ratio"
    risk_measure: Literal["variance", "semi_variance", "cvar", "evar", "cdar", "ulcer", "worst"] = "cvar"
    cv_scheme: Literal["walkforward", "combinatorial", "randomized"] | None = None
    lookback_days: int = Field(default=730, ge=60, le=3650)
    weights_constraint: dict[str, Any] | None = None


class OptimStressRequest(BaseModel):
    n_scenarios: int = Field(default=500, ge=100, le=5000)


# ---------------------------------------------------------------------------
# ML Studio
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RL Lab
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Qlib
# ---------------------------------------------------------------------------

class QlibRunRequest(BaseModel):
    instruments: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "GOOGL"])
    alpha_set: Literal["alpha158", "alpha101", "custom"] = "alpha158"
    model_type: Literal["lgbm", "linear"] = "lgbm"
    start_date: str = "2018-01-01"
    end_date: str = "2022-12-31"


class TransactionOut(BaseModel):
    """Response schema for transaction endpoints."""
    id: str
    holding_id: str
    type: str
    date: DateType
    quantity: Decimal
    price: Decimal
    fees: Decimal
    notes: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TransactionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_id: str
    type: str
    date: DateType
    quantity: Decimal
    price: Decimal
    fees: Decimal = Decimal("0")
    notes: Optional[str] = None


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Optional[str] = Field(default=None)
    date: Optional[DateType] = Field(default=None)
    quantity: Optional[Decimal] = Field(default=None)
    price: Optional[Decimal] = Field(default=None)
    fees: Optional[Decimal] = Field(default=None)
    notes: Optional[str] = Field(default=None)


class TelegramPairingCodeOut(BaseModel):
    code: str
    expires_at: str


class TelegramLinkStatusOut(BaseModel):
    linked: bool
    linked_at: str | None = None


# --- Budget router response models (audit-fixes-2026-08 todo 32) ---
# Money fields are float, NOT Decimal: these routes historically serialized via
# jsonable_encoder (Decimal -> float). A Decimal-typed field would retype the
# payload to a JSON string and break frontend consumers. Golden shapes are locked
# by backend/tests/fixtures/budget_golden_responses.json + parity test.


class BudgetSummaryOut(BaseModel):
    year: int
    month: int
    currency: str
    spent: float
    income: float
    net_income: float
    active_subscription_run_rate: float
    open_invoice_total: float
    open_invoice_count: int
    expense_count: int
    investable_surplus_estimate: float


class SubscriptionOut(OrmModel):
    id: str
    user_id: str
    name: str
    amount: float
    currency: str
    billing_cycle: str
    next_due_date: date
    category_id: str | None = None
    payment_method: str | None = None
    logo_url: str | None = None
    active: bool
    notes: str | None = None


class SubscriptionSuggestionOut(BaseModel):
    name: str
    amount: float
    currency: str
    billing_cycle: str
    next_due_date: str
    source: str
    occurrences: int


class IncomeSourceOut(OrmModel):
    id: str
    user_id: str
    name: str
    amount: float
    currency: str
    cadence: str
    next_date: date
    active: bool


class InvoiceOut(OrmModel):
    id: str
    user_id: str
    issuer: str
    amount: float
    currency: str
    due_date: date | None = None
    paid: bool
    notes: str | None = None


class CategoryOut(OrmModel):
    id: str
    user_id: str
    name: str
    color: str
    icon: str
    type: str
    target_amount: float | None = None
    target_date: date | None = None


class CashflowMonthOut(BaseModel):
    month: str
    income: float
    spend: float


class CategorizeRunOut(BaseModel):
    rule_categorized: int
    llm_categorized: int


class SinkingFundGoalOut(BaseModel):
    target_amount: float
    target_date: str
    progress: float
    required_monthly: float


class EnvelopeStatusOut(BaseModel):
    category_id: str
    category_name: str
    color: str
    icon: str
    budgeted: float
    spent: float
    remaining: float
    overspent: bool
    goal: SinkingFundGoalOut | None = None


class EnvelopesOut(BaseModel):
    money_to_budget: float
    envelopes: list[EnvelopeStatusOut]


class EnvelopeBudgetSetOut(BaseModel):
    category_id: str
    year: int
    month: int
    budgeted_amount: float


class EnvelopeCoverOut(BaseModel):
    from_category_id: str
    from_remaining_budgeted: float
    to_category_id: str
    to_remaining_budgeted: float
    amount: float


class EnvelopeHistoryMonthOut(BaseModel):
    year: int
    month: int
    budgeted: float
    spent: float
    over: float


class EnvelopeHistoryRowOut(BaseModel):
    category_id: str
    category_name: str
    color: str
    months: list[EnvelopeHistoryMonthOut]
    months_over_budget: int
    months_with_budget: int
    consistently_over: bool
    consistently_under: bool


class BufferMetricOut(BaseModel):
    buffer_amount: float
    avg_daily_spend: float
    buffer_days: float | None = None


class SankeyLinkOut(BaseModel):
    source: str
    target: str
    value: float


class AnnualSankeyOut(BaseModel):
    year: int
    total_income: float
    total_expense: float
    links: list[SankeyLinkOut]


class ReviewUncategorizedExpenseOut(BaseModel):
    id: str
    date: str
    amount: float
    currency: str
    description: str
    source: str
    notes: str | None = None


class ReviewDuplicatePairOut(BaseModel):
    manual_expense_id: str
    manual_description: str
    manual_date: str
    synced_expense_id: str
    synced_description: str
    synced_date: str
    amount: float


class ReviewSinkingFundOut(SinkingFundGoalOut):
    category_id: str
    category_name: str


class MonthlyReviewOut(BaseModel):
    year: int
    month: int
    uncategorized_expenses: list[ReviewUncategorizedExpenseOut]
    potential_duplicates: list[ReviewDuplicatePairOut]
    sinking_funds: list[ReviewSinkingFundOut]


# ---------------------------------------------------------------------------
# Tax router response models (audit-fixes-2026-08 todo 33)
# ---------------------------------------------------------------------------

# Gated tax actions degrade to the disabled payload (estimate/not_tax_advice/
# disabled/reason + structural `lines`/`tax`/`provenance` empties), while the
# enabled shape of the same route carries its own keys. One model per route
# therefore declares the WIDEST union of its variants; absent variants default
# to None. `lines` is a dict when computed and a bare list when disabled.
LinesPayload = list[Any] | dict[str, Any]


class TaxSettingsEnvelope(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class BasiszinsItemOut(BaseModel):
    year: int
    basiszins: float


class BasiszinsListOut(BaseModel):
    items: list[BasiszinsItemOut]


class KapReportOut(BaseModel):
    """GET /reports/kap — enabled Anlage-KAP estimate or gated empty payload."""

    tax_year: int | None = None
    form: str | None = None
    estimate: bool = True
    not_tax_advice: bool = True
    lines: LinesPayload | None = None
    tax: dict[str, Any] | None = None
    allowance: dict[str, Any] | None = None
    carryforward: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    disabled: bool | None = None
    reason: str | None = None


class TaxEventRowOut(BaseModel):
    id: str
    tax_year: int
    event_date: str
    event_type: str
    isin: str | None = None
    symbol: str | None = None
    name: str | None = None
    fund_class: str | None = None
    teilfreistellung_pct: float
    gross_eur: float
    withheld_eur: float
    foreign_wht_eur: float
    foreign_country: str | None = None
    realised_gain_eur: float | None = None
    bucket: str | None = None
    confidence: str
    source: str
    source_ref: str | None = None
    institution: str | None = None
    notes: str | None = None


class TaxEventsListOut(BaseModel):
    items: list[TaxEventRowOut]
    estimate: bool = True
    not_tax_advice: bool = True


class DeletedOut(BaseModel):
    deleted: str


class TaxLotRowOut(BaseModel):
    id: str
    isin: str
    symbol: str | None = None
    name: str | None = None
    fund_class: str
    teilfreistellung_pct: float
    acquired_at: str
    quantity_initial: float
    quantity_remaining: float
    cost_basis_eur: float
    fees_eur: float
    source: str
    source_ref: str | None = None
    closed_at: str | None = None
    confidence: str


class TaxLotsListOut(BaseModel):
    items: list[TaxLotRowOut]
    estimate: bool = True
    not_tax_advice: bool = True


class TaxMutationOut(BaseModel):
    """Wide union for /api/tax POST mutations.

    Happy mutations carry their identifying keys (id / created / realised
    gains / residency fields); gated actions degrade to the disabled payload
    with structural empties. The mandated flags are always present.
    """

    id: str | None = None
    created: int | None = None
    country: str | None = None
    valid_from: str | None = None
    realised_gain_eur: float | None = None
    gross_eur: float | None = None
    estimate: bool = True
    not_tax_advice: bool = True
    disabled: bool | None = None
    reason: str | None = None
    lines: LinesPayload | None = None
    tax: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None


class VorabClassificationOut(BaseModel):
    value: str
    source: str


class VorabpauschaleEstimateOut(BaseModel):
    """POST /vorabpauschale/estimate — §18 estimate or gated empty payload."""

    basiszins: float | None = None
    basiszins_known: bool | None = None
    basisertrag_eur: float | None = None
    fund_gain_eur: float | None = None
    distributions_eur: float | None = None
    vorabpauschale_eur: float | None = None
    months_held: int | None = None
    tax_year: int | None = None
    confidence: str | None = None
    isin: str | None = None
    fund_class: str | None = None
    classification: VorabClassificationOut | None = None
    teilfreistellung_pct: float | None = None
    taxable_after_teilfreistellung_eur: float | None = None
    estimate: bool = True
    not_tax_advice: bool = True
    disabled: bool | None = None
    reason: str | None = None
    lines: LinesPayload | None = None
    tax: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None


class JurisdictionOut(BaseModel):
    code: str
    name: str
    label: str


class JurisdictionsOut(BaseModel):
    jurisdictions: list[JurisdictionOut]


class ResidencyPeriodOut(BaseModel):
    id: str
    country: str
    valid_from: str
    valid_to: str | None = None


class ResidencyListOut(BaseModel):
    periods: list[ResidencyPeriodOut]


class TaxOverviewOut(BaseModel):
    """GET /overview and GET /box3 — jurisdiction-routed overview payloads.

    DE resolves to the KAP year overview (allowance/lines/tax/carryforward/
    anlage_kap_mapping/vorabpauschale_estimate/provenance), NL to the Box 3
    wealth overview (wealth/calculation), unsupported jurisdictions and gated
    states to the disabled payload.
    """

    tax_year: int | None = None
    jurisdiction: str | None = None
    label: str | None = None
    estimate: bool = True
    not_tax_advice: bool = True
    allowance: dict[str, Any] | None = None
    lines: LinesPayload | None = None
    tax: dict[str, Any] | None = None
    carryforward: dict[str, Any] | None = None
    anlage_kap_mapping: dict[str, Any] | None = None
    vorabpauschale_estimate: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    wealth: dict[str, Any] | None = None
    calculation: dict[str, Any] | None = None
    disabled: bool | None = None
    reason: str | None = None
    # DE only: withheld by the banks vs. owed after NV and Guenstigerpruefung
    # (tax_allowances.tax_position).
    position: dict[str, Any] | None = None


class GainHarvestStepOut(BaseModel):
    isin: str
    name: str | None = None
    sell_quantity: float
    notional_eur: float
    gain_eur: float
    taxable_gain_eur: float
    trading_cost_eur: float
    future_tax_avoided_eur: float
    net_benefit_eur: float
    whole_position: bool
    bank: str | None = None


class GainHarvestNoticeOut(BaseModel):
    code: str
    message: str


class GainHarvestOut(BaseModel):
    """GET /harvest — tax-free gain harvesting under an NV certificate (estimate)."""

    tax_year: int
    label: str | None = None
    estimate: bool = True
    not_tax_advice: bool = True
    enabled: bool = False
    reason: str | None = None
    # "nv" (NV certificate: room up to the Grundfreibetrag) or "allowance" (only
    # what each bank's Freistellungsauftrag still covers).
    mode: str | None = None
    nv_certificate: dict[str, Any] | None = None
    health_insurance: str | None = None
    room: dict[str, Any] | None = None
    bank_rooms: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[GainHarvestStepOut] = Field(default_factory=list)
    totals: dict[str, float] | None = None
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[GainHarvestNoticeOut] = Field(default_factory=list)


class TaxAllowanceActionOut(BaseModel):
    code: str
    severity: str  # action | warning | info
    title: str
    message: str
    bank: str | None = None
    deadline: str | None = None
    how_to: str | None = None


class TaxAllowanceBankOut(BaseModel):
    bank: str
    label: str
    fsa_eur: float | None = None
    nv_filed: bool = False
    nv_covers: bool = False
    booked_eur: float = 0
    used_eur: float = 0
    remaining_eur: float = 0
    projected_eur: float = 0
    projected_uncovered_eur: float = 0
    projected_withheld_eur: float = 0
    minimum_fsa_eur: float = 0
    recommended_fsa_eur: float = 0
    recommended_withheld_eur: float = 0
    change_eur: float = 0
    booked: dict[str, float | None] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    vorabpauschale_estimated_eur: float | None = None
    savings_balance_eur: float | None = None
    interest_rate: float | None = None
    loss_left: dict[str, float | None] = Field(default_factory=dict)
    harvestable_gain_eur: float | None = None
    how_to: dict[str, str] = Field(default_factory=dict)


class TaxAllowancePlanOut(BaseModel):
    """GET /allowances — Freistellungsauftrag and NV certificate per bank (estimate)."""

    tax_year: int
    label: str | None = None
    estimate: bool = True
    not_tax_advice: bool = True
    applicable: bool = False
    reason: str | None = None
    allowance_eur: float | None = None
    other_banks_eur: float | None = None
    available_eur: float | None = None
    assigned_eur: float | None = None
    over_assigned: bool = False
    withholding_rate: float | None = None
    projected_withheld_eur: float | None = None
    recommended_withheld_eur: float | None = None
    changes_needed: bool = False
    projected_capital_income_eur: float | None = None
    other_banks_income_eur: float | None = None
    unassigned: dict[str, Any] = Field(default_factory=dict)
    nv: dict[str, Any] = Field(default_factory=dict)
    banks: list[TaxAllowanceBankOut] = Field(default_factory=list)
    actions: list[TaxAllowanceActionOut] = Field(default_factory=list)
    sources: list[dict[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Portfolio -> Risk response models (GET /api/verification/risk|stress/{id})
# ---------------------------------------------------------------------------


class RiskAlertOut(BaseModel):
    id: str
    type: str
    severity: str
    title: str
    message: str
    created_at: datetime


class ReturnBasisOut(BaseModel):
    """What the VaR / drawdown / Sharpe numbers were computed from."""

    source: str = "holdings_price_history"
    n_obs: int = 0
    start: str | None = None
    end: str | None = None
    priced_assets: list[str] = Field(default_factory=list)
    missing_history: list[str] = Field(default_factory=list)
    message: str | None = None


class RiskAssessmentOut(BaseModel):
    var_95: float
    var_99: float
    max_drawdown: float
    current_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    concentration_index: float
    lookthrough_hhi: float
    lookthrough_count: int
    asset_type_exposure: dict[str, float] = Field(default_factory=dict)
    currency_exposure: dict[str, float] = Field(default_factory=dict)
    alerts: list[RiskAlertOut] = Field(default_factory=list)
    insufficient_history: bool = False
    return_basis: ReturnBasisOut = Field(default_factory=ReturnBasisOut)


class StressScenarioResultOut(BaseModel):
    scenario_name: str
    description: str = ""
    portfolio_impact_pct: float
    worst_case_loss: float
    details: dict[str, float] = Field(default_factory=dict)


class StressTestResultOut(BaseModel):
    portfolio_id: str
    scenarios: list[StressScenarioResultOut] = Field(default_factory=list)
    note: str = ""
    created_at: str
