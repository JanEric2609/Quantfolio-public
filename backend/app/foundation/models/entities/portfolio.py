from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")

    holdings: Mapped[list["Holding"]] = relationship(back_populates="portfolio", lazy="selectin")


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("portfolio_id", "isin", name="uq_holdings_portfolio_isin"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_type: Mapped[str] = mapped_column(String(24), default="stock")
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    avg_buy_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    buy_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    source: Mapped[str] = mapped_column(String(24), default="manual")
    dkb_available: Mapped[bool] = mapped_column(default=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="holdings", lazy="selectin")
    transactions: Mapped[list["TransactionLog"]] = relationship(
        back_populates="holding", lazy="selectin"
    )


class TransactionLog(Base):
    __tablename__ = "transactions_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    holding_id: Mapped[str] = mapped_column(
        ForeignKey("holdings.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(20))
    date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    fees: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    notes: Mapped[str | None] = mapped_column(Text)

    holding: Mapped[Holding] = relationship(back_populates="transactions", lazy="selectin")


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "date", "source", name="uq_portfolio_snapshots_portfolio_date_source"
        ),
        UniqueConstraint(
            "user_id", "date", "source", name="uq_portfolio_snapshots_user_date_source"
        ),
        Index("ix_portfolio_snapshots_portfolio_date", "portfolio_id", "date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_id: Mapped[str | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    total_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    cash_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    security_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    total_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    source: Mapped[str] = mapped_column(String(32), default="computed")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class BookPositionSnapshot(Base):
    """One position at one broker at the end of one day, for every synced broker.

    The quantity history the book's time-weighted return is built from
    (``foundation/book_performance.py``). DKB's FinTS sync reports no trades,
    so a day's holdings are the only record of what was bought; Scalable's
    trades are in the activity ledger, but its snapshots keep both brokers on
    one footing. Written after every sync and by the daily snapshot job.
    """

    __tablename__ = "book_position_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "snapshot_date", "source", "account_id", "isin", name="uq_book_position_snapshot"),
        Index("ix_book_position_snapshots_user_date", "user_id", "snapshot_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32))  # "dkb", "scalable"
    # A DKB depot or a connected broker account; no FK, it can be either.
    account_id: Mapped[str] = mapped_column(String(36))
    isin: Mapped[str] = mapped_column(String(12))
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ShadowPosition(Base):
    """Shadow portfolio position (Phase 5). Mirrors real portfolio evolution."""

    __tablename__ = "shadow_positions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    mtm: Mapped[Decimal] = mapped_column(Numeric(20, 6))


class Benchmark(Base):
    __tablename__ = "benchmarks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ticker: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))


class ConnectedAccount(Base):
    __tablename__ = "connected_accounts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "external_id", name="uq_connected_accounts_user_source_external"
        ),
        Index("ix_connected_accounts_user_source", "user_id", "source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    external_id: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(180))
    institution: Mapped[str | None] = mapped_column(String(120))
    account_type: Mapped[str] = mapped_column(String(32), default="cash")
    iban: Mapped[str | None] = mapped_column(String(34), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    last_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ActivityLedgerEntry(Base):
    __tablename__ = "activity_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "dedupe_hash", name="uq_activity_ledger_user_source_hash"
        ),
        Index("ix_activity_ledger_user_date", "user_id", "date"),
        Index("ix_activity_ledger_user_review", "user_id", "review_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connected_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("connected_accounts.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    external_id: Mapped[str | None] = mapped_column(String(120))
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)
    activity_type: Mapped[str] = mapped_column(String(32), default="cashflow")
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    description: Mapped[str] = mapped_column(String(300))
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    fees: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    review_state: Mapped[str] = mapped_column(String(24), default="trusted", index=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
