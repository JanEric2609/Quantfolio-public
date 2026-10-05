from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text


class Composite(Base):
    """User-defined grouping of portfolios for consolidated performance reporting (Phase 3)."""

    __tablename__ = "composites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    definition_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )


class CompositeMembership(Base):
    """Many-to-many: composite -> portfolio with validity periods (Phase 3)."""

    __tablename__ = "composite_membership"

    composite_id: Mapped[str] = mapped_column(
        ForeignKey("composites.id", ondelete="CASCADE"), primary_key=True
    )
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class PerformanceLedgerEntry(Base):
    """Performance ledger entry: TWR, MWR, dispersion for composite (Phase 3, hypertable)."""

    __tablename__ = "performance_ledger_entries"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    composite_id: Mapped[str] = mapped_column(
        ForeignKey("composites.id", ondelete="CASCADE"), index=True
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    twr: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    mwr: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    dispersion: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    ex_post_risk_json: Mapped[str] = mapped_column(Text)
    snapshot_meta_json: Mapped[str] = mapped_column(Text)
