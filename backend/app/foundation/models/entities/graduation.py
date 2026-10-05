from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class GraduationAssessment(Base):
    """A point-in-time verdict on whether the LLM has earned the right to
    advise the user's real portfolio. Persisted so progress can be trended.
    """

    __tablename__ = "graduation_assessments"
    __table_args__ = (Index("ix_graduation_assessments_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    graduated: Mapped[bool] = mapped_column(Boolean, default=False)
    overall_progress: Mapped[float] = mapped_column(Float, default=0.0)
    criteria_json: Mapped[str] = mapped_column(Text, default="[]")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
