from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text


class PaperPortfolio(Base):
    __tablename__ = "paper_portfolios"
    __table_args__ = (
        # Old constraint dropped in migration 0055; replaced by uq_paper_portfolio_user_mandate
        # UniqueConstraint("user_id", name="uq_paper_portfolio_user"),
        UniqueConstraint("user_id", "mandate", name="uq_paper_portfolio_user_mandate"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="AI Paper Portfolio")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("100000"))
    # Starting invested capital = initial cash + cost basis of seeded holdings.
    # This is the basis for total_return_pct; initial_cash alone is only the cash
    # sleeve and must not be used as the return basis (see migration 0060).
    baseline_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    mandate: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=sa_text("'manual'")
    )
    managed_by: Mapped[str] = mapped_column(String(24), default="manual")
    mandate_config_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    # Start of the current run; a reset moves it and archives what came before.
    inception_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    # EUR price of the benchmark at the moment the run was seeded, so the
    # passive benchmark starts at the same instant and price source as the
    # sleeve. NULL on older rows (the close on/before inception is used).
    benchmark_base_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    benchmark_base_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    holdings: Mapped[list["PaperHolding"]] = relationship(
        back_populates="portfolio", lazy="selectin"
    )
    trades: Mapped[list["PaperTrade"]] = relationship(back_populates="portfolio", lazy="selectin")
    snapshots: Mapped[list["PaperSnapshot"]] = relationship(
        back_populates="portfolio", lazy="selectin"
    )


class PaperHolding(Base):
    __tablename__ = "paper_holdings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Canonical values from app.foundation.instrument_taxonomy: "stock" (equity), "etf", "money_market", "bond".
    asset_type: Mapped[str] = mapped_column(String(24), default="stock")
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    avg_buy_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    portfolio: Mapped[PaperPortfolio] = relationship(back_populates="holdings", lazy="selectin")
    trades: Mapped[list["PaperTrade"]] = relationship(back_populates="holding", lazy="selectin")


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    holding_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_holdings.id", ondelete="SET NULL"), nullable=True
    )
    ticker: Mapped[str] = mapped_column(String(32))
    # What was traded, recorded at execution: a sell that closes a position
    # deletes the holding, so the trade check could no longer tell an ETF
    # from a stock (plan 2026-10-14, M2).
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    asset_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # A sale's cost basis: the holding's average buy price just before it. A
    # seeded position has no buy trade, so once sold out it cannot be rebuilt.
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    value: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    # Commission charged on this order. A buy costs value+fee in cash, a sell
    # returns value-fee, so cash stays reconstructible from the trade log.
    fee: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), default=Decimal("0"), server_default=sa_text("0")
    )
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    portfolio: Mapped[PaperPortfolio] = relationship(back_populates="trades", lazy="selectin")
    holding: Mapped[PaperHolding | None] = relationship(back_populates="trades", lazy="selectin")


class PaperTradeCheck(Base):
    """The trade check's verdict on one paper trade, in shadow (decision/trade_check.py).

    Written after the fact for every trade a managed sleeve booked: the
    trade's cost (fee, half spread, tax), the edge its source has shown so
    far, and whether the check would have blocked it. Nothing is enforced:
    the paper book trades as before. ``contribution_eur`` is what the trade
    added against MSCI World over its 21-trading-day horizon, net of cost;
    it is marked daily until ``horizon_end`` and then frozen (``resolved``).
    """

    __tablename__ = "paper_trade_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    trade_id: Mapped[str] = mapped_column(
        ForeignKey("paper_trades.id", ondelete="CASCADE"), unique=True, index=True
    )
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    traded_on: Mapped[date] = mapped_column(Date)
    ticker: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    notional_eur: Mapped[float] = mapped_column(Float)
    fee_eur: Mapped[float] = mapped_column(Float, default=0.0)
    spread_eur: Mapped[float] = mapped_column(Float, default=0.0)
    tax_eur: Mapped[float] = mapped_column(Float, default=0.0)
    cost_eur: Mapped[float] = mapped_column(Float, default=0.0)
    p_hit: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_source: Mapped[str] = mapped_column(String(16))
    mean_abs_excess: Mapped[float] = mapped_column(Float)
    edge_eur: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(Text)
    horizon_end: Mapped[date] = mapped_column(Date)
    asset_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    contribution_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PaperSnapshot(Base):
    __tablename__ = "paper_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "date", name="uq_paper_snapshots_portfolio_date"),
        Index("ix_paper_snapshots_portfolio_date", "portfolio_id", "date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date] = mapped_column(Date, index=True)
    total_value: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("0"))
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("0"))
    securities_value: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("0"))
    total_return_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0"))
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    portfolio: Mapped[PaperPortfolio] = relationship(back_populates="snapshots")


class LlmPortfolioDecision(Base):
    __tablename__ = "llm_portfolio_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    review_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mandate: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decision_json: Mapped[str] = mapped_column(Text, default="{}")
    reflection_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Outcome scoring (filled by the weekly scoring job once horizon_weeks elapses).
    # verdict ∈ {hit, miss, partial}; horizon_weeks comes from the decision's
    # structured expectation. See services/llm_portfolio/scoring.py.
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    horizon_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class LlmAdviceCard(Base):
    __tablename__ = "llm_advice_cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True
    )
    divergence_json: Mapped[str] = mapped_column(Text, default="{}")
    advice_text: Mapped[str] = mapped_column(Text, default="")
    performance_delta: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PaperCashFlow(Base):
    """Cash a paper portfolio receives without trading: dividends (net of source withholding tax, in EUR)."""

    __tablename__ = "paper_cash_flows"
    __table_args__ = (UniqueConstraint("portfolio_id", "kind", "ticker", "date", name="uq_paper_cash_flow"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))
    ticker: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    amount_per_unit: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    currency: Mapped[str] = mapped_column(String(3))
    amount_eur: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PaperPortfolioArchive(Base):
    """Everything a reset took out of a paper portfolio, as one JSON payload."""

    __tablename__ = "paper_portfolio_archives"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    inception_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="reset")
    payload_json: Mapped[str] = mapped_column(Text)
