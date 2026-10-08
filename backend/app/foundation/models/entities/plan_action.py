from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import JSON, DateTime, Float, ForeignKey, String
import sqlalchemy as sa


class MonthlyPlanAction(Base):
    """One action of one month's plan, so a later sync can link a placed order back to it.

    Written by the plan snapshot (one row per computed action, idempotent per
    month); ``status`` moves open → fulfilled when ``detect_executions``
    finds the trade, or open → expired when the month passes. Advisory only:
    rows never place orders.
    """

    __tablename__ = "monthly_plan_actions"
    __table_args__ = (
        sa.UniqueConstraint(
            "user_id", "month", "sleeve", "kind", "isin", "ticker", "broker", "account_id",
            name="uq_plan_action_month_item",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # "YYYY-MM"
    sleeve: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))  # savings_plan | order | sale | one_off
    amount_eur: Mapped[float] = mapped_column(Float, default=0.0)
    instrument: Mapped[str] = mapped_column(String(128), default="")
    # Empty string, never NULL: NULLs would silently defeat the per-month
    # idempotency constraint (NULL != NULL).
    ticker: Mapped[str] = mapped_column(String(32), default="")
    isin: Mapped[str] = mapped_column(String(16), default="")
    broker: Mapped[str] = mapped_column(String(16), default="")
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | fulfilled | expired
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
