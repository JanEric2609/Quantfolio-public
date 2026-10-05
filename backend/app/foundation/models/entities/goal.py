from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    target_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    progress: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Goal-based portfolio construction fields
    risk_tolerance: Mapped[str | None] = mapped_column(
        String(24), nullable=True
    )  # conservative / moderate / aggressive
    asset_class_targets: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON dict e.g. {"equity": 60, "bonds": 30, "cash": 10}
    monthly_contribution: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
