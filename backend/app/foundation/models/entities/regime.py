from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from sqlalchemy import JSON, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint


class RegimeRecommendationWeight(Base):
    """MWU weight for a regime × action pair (Phase 4: Closed-Loop Refinement)."""

    __tablename__ = "regime_recommendation_weights"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    regime_label: Mapped[str] = mapped_column(String(24), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    n_obs: Mapped[int] = mapped_column(Integer, default=0)
    cum_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint("regime_label", "action", name="uq_regime_action"),
        Index("ix_regime_weights_label_action", "regime_label", "action"),
    )


class FactorIcTracking(Base):
    """Information Coefficient tracking per regime × factor (Phase 4)."""

    __tablename__ = "factor_ic_tracking"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    regime_label: Mapped[str] = mapped_column(String(24), index=True)
    factor_name: Mapped[str] = mapped_column(String(64), index=True)
    ic_value: Mapped[float] = mapped_column(Float, default=0.0)
    t_stat: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_obs: Mapped[int] = mapped_column(Integer, default=0)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    notes_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_factor_ic_tracking_regime_factor", "regime_label", "factor_name"),)


class RegimeLabelHistory(Base):
    """One regime call per day, frozen when the daily regime job issued it.

    ``regime_snapshots`` is the raw job output (several rows per day possible,
    purgeable, no ORM entity); this is the ledger the "Can I trust it?" page
    scores against: exactly one label per calendar day, written by
    ``lab/regime/history.record_regime_label`` from ``classify_and_store``.
    ``as_of`` is the day the call was issued (UTC), not the feature row's date.
    """

    __tablename__ = "regime_label_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    as_of: Mapped[date] = mapped_column(Date, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(24))
    probabilities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
