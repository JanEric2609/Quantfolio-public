from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text


class Nudge(Base):
    """Nudge notification for high-conviction recommendations (Phase 5)."""

    __tablename__ = "nudges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(
        String(40), index=True
    )  # "high_conviction_rec", "regime_change", "drift_breach", ...
    severity: Mapped[str] = mapped_column(
        String(24), default="info"
    )  # "info", "warning", "critical"
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
