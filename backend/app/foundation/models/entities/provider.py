from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint


class ProviderHealth(Base):
    __tablename__ = "provider_health"
    __table_args__ = (
        UniqueConstraint("provider", "capability", name="uq_provider_health_provider_capability"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    capability: Mapped[str] = mapped_column(String(40), default="status", index=True)
    configured: Mapped[bool] = mapped_column(Boolean, default=False)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    message: Mapped[str] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
