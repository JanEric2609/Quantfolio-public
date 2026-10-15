"""The plan's one-year bands, stored before the year starts, and their scores (plan 2026-10-14, D6).

Append-only: one band per user, model and month, never changed; its score row
is added once the year has passed and the book's return and inflation exist.
"""
from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class PlanBandForecast(Base):
    """The 10/50/90 % band of the book's real return over the next year, and the value it implies."""

    __tablename__ = "plan_band_forecasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: ``planner`` (the projection's parametric model) or ``long_history`` (the JST bootstrap).
    model: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(40))
    forecast_day: Mapped[date] = mapped_column(Date)
    end_day: Mapped[date] = mapped_column(Date)
    #: Real return of the book over the year, as fractions.
    p10: Mapped[float] = mapped_column(Float)
    p50: Mapped[float] = mapped_column(Float)
    p90: Mapped[float] = mapped_column(Float)
    start_value: Mapped[float] = mapped_column(Float)
    monthly_contribution: Mapped[float] = mapped_column(Float)
    #: The book's value in a year in today's euros, the planned contributions included.
    value_p10: Mapped[float] = mapped_column(Float)
    value_p50: Mapped[float] = mapped_column(Float)
    value_p90: Mapped[float] = mapped_column(Float)
    #: The model's inputs: real return, volatility, mix, homes, source.
    inputs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "model", "forecast_day", name="uq_plan_band_user_model_day"),
    )


class PlanBandScore(Base):
    """What the book did over a band's year, in real terms."""

    __tablename__ = "plan_band_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    forecast_id: Mapped[str] = mapped_column(
        ForeignKey("plan_band_forecasts.id", ondelete="CASCADE"), unique=True, index=True
    )
    #: The unit-value days actually used (the nearest on or before each end of the year).
    start_day: Mapped[date] = mapped_column(Date)
    end_day: Mapped[date] = mapped_column(Date)
    nominal_return: Mapped[float] = mapped_column(Float)
    #: Euro-area HICP, % change on a year earlier, for the end month, as a fraction.
    inflation: Mapped[float] = mapped_column(Float)
    real_return: Mapped[float] = mapped_column(Float)
    inside: Mapped[bool] = mapped_column(Boolean)
    #: -1 below the 10 % line, 0 inside, +1 above the 90 % line.
    side: Mapped[int] = mapped_column(Integer)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
