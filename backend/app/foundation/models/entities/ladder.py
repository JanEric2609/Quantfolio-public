"""Capital ladder state (ADR 0025, plan 2026-11-10 WS4)."""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk



class LadderCandidate(Base):
    """One epoch of one capital-ladder slot (ADR 0025 WS4).

    ``slot`` is ``llm`` (the advisor champion lineage) or ``rules`` (Mandate B);
    ``kind`` says what ``ref_id`` points at (an advisor strategy or a paper
    portfolio). A changed configuration, a paper reset, a demotion to stage 0
    or the 24-month expiry retires the row and starts epoch + 1.
    """

    __tablename__ = "ladder_candidates"
    __table_args__ = (UniqueConstraint("user_id", "slot", "epoch", name="uq_ladder_candidate_epoch"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slot: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[str] = mapped_column(String(36))
    portfolio_id: Mapped[str] = mapped_column(String(36), index=True)
    config_hash: Mapped[str] = mapped_column(String(64))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    pi: Mapped[float] = mapped_column(Float)
    born_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stage: Mapped[int] = mapped_column(Integer, default=0)
    stage_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vetoed: Mapped[bool] = mapped_column(Boolean, default=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class LadderEvidenceDaily(Base):
    """The ladder's nightly evidence for one candidate on one day (ADR 0025 WS4)."""

    __tablename__ = "ladder_evidence_daily"
    __table_args__ = (UniqueConstraint("candidate_id", "day", name="uq_ladder_evidence_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("ladder_candidates.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date)
    x: Mapped[float | None] = mapped_column(Float, nullable=True)
    sigma: Mapped[float | None] = mapped_column(Float, nullable=True)
    e_skill: Mapped[float | None] = mapped_column(Float, nullable=True)
    e_harm: Mapped[float | None] = mapped_column(Float, nullable=True)
    rel_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    te: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage: Mapped[int] = mapped_column(Integer, default=0)
