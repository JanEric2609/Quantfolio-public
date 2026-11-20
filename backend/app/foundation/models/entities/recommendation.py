from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime, Float, ForeignKey, Integer, Numeric, String, Text


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    horizon: Mapped[str] = mapped_column(String(24), default="mid")
    verdict: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=0)
    payload_json: Mapped[str] = mapped_column(Text)
    backtest_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    recommendation_v2_payload_json: Mapped[str | None] = mapped_column(Text)
    recommendation_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    approval_state: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    mode: Mapped[str | None] = mapped_column(String(24), index=True)
    data_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    portfolio_fit_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))


class RecommendationReview(Base):
    __tablename__ = "recommendation_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class RecommendationOutcome(Base):
    """Outcome evaluation for a portfolio advisor recommendation rec."""

    __tablename__ = "recommendation_outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), index=True, unique=True
    )
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    window_days: Mapped[int] = mapped_column(Integer, default=60)
    abs_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    benchmark_excess_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    portfolio_sortino_delta: Mapped[float | None] = mapped_column(Float)
    outcome_label: Mapped[str] = mapped_column(String(16), default="pending")
    notes_json: Mapped[str | None] = mapped_column(Text)


class RecommendationAttempt(Base):
    __tablename__ = "recommendation_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    horizon: Mapped[str] = mapped_column(String(24), default="mid")
    status: Mapped[str] = mapped_column(String(24), default="failed")
    error_message: Mapped[str | None] = mapped_column(Text)
    debug_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
