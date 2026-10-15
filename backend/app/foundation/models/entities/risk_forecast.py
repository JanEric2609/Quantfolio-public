"""Risk forecasts stored before the fact, and their scores once the horizon has passed (plan 2026-10-14, D3).

Append-only: a forecast row is written once per target, day and horizon and
never changed; its score row is added when the horizon's returns exist.
"""
from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class RiskForecastEntry(Base):
    """One target's volatility, VaR and ES for the next ``horizon_days`` trading days, as of ``forecast_day``'s close."""

    __tablename__ = "risk_forecasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    #: ``paper:<portfolio id>``, ``book:<user id>`` or ``benchmark:<ticker>``.
    target: Mapped[str] = mapped_column(String(80), index=True)
    label: Mapped[str] = mapped_column(String(160))
    forecast_day: Mapped[date] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(40))
    model_hash: Mapped[str] = mapped_column(String(16))
    #: Fractions of the target's value; losses positive.
    sigma: Mapped[float] = mapped_column(Float)
    var95: Mapped[float] = mapped_column(Float)
    var975: Mapped[float] = mapped_column(Float)
    es975: Mapped[float] = mapped_column(Float)
    n_obs: Mapped[int] = mapped_column(Integer)
    n_calibration: Mapped[int] = mapped_column(Integer)
    #: The weights frozen on ``forecast_day`` (symbol -> share, ``cash`` included); the score uses them.
    weights_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        sa.UniqueConstraint("target", "forecast_day", "horizon_days", name="uq_risk_forecast_target_day_h"),
    )


class RiskForecastScore(Base):
    """What happened over a forecast's horizon, with its e-backtest values."""

    __tablename__ = "risk_forecast_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    forecast_id: Mapped[str] = mapped_column(
        ForeignKey("risk_forecasts.id", ondelete="CASCADE"), unique=True, index=True
    )
    end_day: Mapped[date] = mapped_column(Date)
    #: The frozen-weight return over the horizon; ``loss`` is its negative.
    realized_return: Mapped[float] = mapped_column(Float)
    loss: Mapped[float] = mapped_column(Float)
    var95_breach: Mapped[bool] = mapped_column(Boolean)
    var_e: Mapped[float] = mapped_column(Float)
    es_e: Mapped[float] = mapped_column(Float)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
