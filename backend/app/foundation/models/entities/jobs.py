from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import DateTime, Integer, String, Text


class JobRun(Base):
    """Scheduled job execution tracking (Phase 5). Records each job run with timing and status."""

    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), default="scheduler")


class AppError(Base):
    """An unexpected error, grouped by fingerprint (one row per distinct error).

    Written by the API's catch-all handler (``source="api"``) and by the
    browser through ``POST /api/errors/client`` (``source="client"``). The
    Control Center's attention list and the daily self-check read the rows
    seen in the last day. A repeat bumps ``count`` and ``last_seen`` instead
    of adding a row, so a loop that fails every minute stays one line.
    """

    __tablename__ = "app_errors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
