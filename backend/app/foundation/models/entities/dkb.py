from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint


class DkbAccount(Base):
    __tablename__ = "dkb_accounts"
    __table_args__ = (UniqueConstraint("user_id", "iban", name="uq_dkb_account_user_iban"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    iban: Mapped[str | None] = mapped_column(String(34), index=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    last_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class DkbTransaction(Base):
    __tablename__ = "dkb_transactions"
    __table_args__ = (UniqueConstraint("account_id", "dedupe_hash", name="uq_dkb_tx_account_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("dkb_accounts.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    reference: Mapped[str] = mapped_column(Text)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(24), default="dkb")
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)


class DkbPosition(Base):
    __tablename__ = "dkb_positions"
    __table_args__ = (
        Index("ix_dkb_positions_account_isin", "account_id", "isin"),
        UniqueConstraint("account_id", "isin", name="uq_dkb_positions_account_isin"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("dkb_accounts.id", ondelete="CASCADE"), index=True
    )
    isin: Mapped[str] = mapped_column(String(12), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    avg_buy_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    last_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class PositionSnapshot(Base):
    """Daily point-in-time snapshot of DKB positions for historical analytics."""

    __tablename__ = "position_snapshots"
    __table_args__ = (Index("ix_position_snapshots_account_date", "account_id", "snapshot_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("dkb_accounts.id", ondelete="CASCADE"), index=True
    )
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    isin: Mapped[str] = mapped_column(String(12))
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    avg_buy_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class DkbSyncLog(Base):
    __tablename__ = "dkb_sync_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    provider: Mapped[str] = mapped_column(String(24), default="fints")
    state: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class DkbDiagnosticRun(Base):
    __tablename__ = "dkb_diagnostic_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(24), default="fints")
    status: Mapped[str] = mapped_column(String(24), default="warning", index=True)
    summary: Mapped[str] = mapped_column(Text)
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    debug_log: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
