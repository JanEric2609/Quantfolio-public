"""Generic live-broker tables (Scalable Capital today, the next broker tomorrow).

DKB keeps its own ``dkb_*`` tables; every broker added after it lands here, so
readers go through ``app.foundation.live_positions`` instead of learning one
more table per broker.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk


class BrokerPosition(Base):
    """One security position at a synced broker account (read-only mirror)."""

    __tablename__ = "broker_positions"
    __table_args__ = (
        UniqueConstraint("connected_account_id", "isin", name="uq_broker_positions_account_isin"),
        Index("ix_broker_positions_user_source", "user_id", "source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connected_account_id: Mapped[str] = mapped_column(
        ForeignKey("connected_accounts.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str] = mapped_column(String(12), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    security_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    pending_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    avg_buy_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    price_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_outdated: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class BrokerSyncLog(Base):
    """One broker sync run: when, how it ended and what it wrote."""

    __tablename__ = "broker_sync_logs"
    __table_args__ = (
        Index("ix_broker_sync_logs_user_source_started", "user_id", "source", "started_at"),
        # One running sync per broker, enforced by the database: two requests
        # (API and worker, or two clicks) cannot both get past the check.
        Index(
            "uq_broker_sync_logs_one_running", "source", unique=True,
            sqlite_where=text("state = 'running'"), postgresql_where=text("state = 'running'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|scheduled
    state: Mapped[str] = mapped_column(String(16), default="running")  # running|success|warning|error
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    counts_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
