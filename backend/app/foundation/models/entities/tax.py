from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy import text as sa_text


class TaxLot(Base):
    """FIFO lot accounting for capital gains.

    Tax estimate only — broker statements remain the source of truth.
    """

    __tablename__ = "tax_lots"
    __table_args__ = (
        Index("ix_tax_lots_user_isin", "user_id", "isin"),
        Index("ix_tax_lots_user_open", "user_id", "closed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    isin: Mapped[str] = mapped_column(String(12), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200))
    account_ref: Mapped[str | None] = mapped_column(String(120))
    fund_class: Mapped[str] = mapped_column(
        String(24), default="other"
    )  # aktien|misch|immobilien|other
    teilfreistellung_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0"))
    acquired_at: Mapped[date] = mapped_column(Date, index=True)
    quantity_initial: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    quantity_remaining: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    cost_basis_eur: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    fees_eur: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(120))
    closed_at: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class TaxLedgerEvent(Base):
    """Canonical tax-relevant events: dividends, interest, sales, Vorabpauschale, withholding."""

    __tablename__ = "tax_ledger_events"
    __table_args__ = (
        Index("ix_tax_ledger_user_year", "user_id", "tax_year"),
        Index("ix_tax_ledger_user_type", "user_id", "event_type"),
        # Idempotent ingestion dedupe (audit-fixes-2026-08 todo 25): synced rows
        # carry source_ref="dkb:<tx-id>"; NULLs (manual events) never collide.
        Index("uq_tax_ledger_source_ref", "source_ref", unique=True),
        Index("ix_tax_ledger_user_institution", "user_id", "institution"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tax_year: Mapped[int] = mapped_column(index=True)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    # dividend|interest|sale|vorabpauschale|withholding|fee
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200))
    fund_class: Mapped[str | None] = mapped_column(String(24))
    teilfreistellung_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0"))
    gross_eur: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    withheld_eur: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    foreign_wht_eur: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    foreign_country: Mapped[str | None] = mapped_column(String(2))
    realised_gain_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    bucket: Mapped[str | None] = mapped_column(String(24))  # aktien|sonstige
    confidence: Mapped[str] = mapped_column(String(16), default="estimate")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(120))
    # The bank that booked it ("dkb" | "scalable" | another name); each bank applies
    # only its own Freistellungsauftrag. NULL: not known (a manual event).
    institution: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class TaxResidencyPeriod(Base):
    """Tax jurisdiction residency period for dual-residency scenarios (Phase 7)."""

    __tablename__ = "tax_residency_periods"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country: Mapped[str] = mapped_column(String(2), nullable=False)  # "DE", "NL", etc.
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)  # None = ongoing
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        Index("ix_tax_residency_periods_user_id_valid_from", "user_id", sa_text("valid_from DESC")),
        Index("ix_tax_residency_periods_user_id_country", "user_id", "country"),
    )
