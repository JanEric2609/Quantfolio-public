from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import DateTime, Float, ForeignKey, String, UniqueConstraint


class TargetAllocation(Base):
    __tablename__ = "target_allocations"
    __table_args__ = (
        UniqueConstraint("user_id", "asset_type", name="uq_target_allocations_user_asset"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(32))
    target_pct: Mapped[float] = mapped_column(Float, default=0.0)
    tolerance_pct: Mapped[float] = mapped_column(Float, default=5.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
