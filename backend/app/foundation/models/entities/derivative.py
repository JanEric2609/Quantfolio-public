"""Warrants and other options held at a broker (ADR 0026).

Scalable's holdings feed names a warrant ("Anthropic Put 100,00 VON") and
types it DERIVATIVE, but carries no expiry, ratio or strike currency, and the
read-only ``sc`` allow-list is not widened to fetch them. The owner enters
those once per ISIN (:class:`DerivativeTerms`); :class:`DerivativeQuote` keeps
one row per warrant and day for the Quantlab's issuer-margin study.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk


class DerivativeTerms(Base):
    """The contract terms of one warrant the owner holds, entered once."""

    __tablename__ = "derivative_terms"
    __table_args__ = (UniqueConstraint("user_id", "isin", name="uq_derivative_terms_user_isin"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    isin: Mapped[str] = mapped_column(String(12), index=True)
    # "put" | "call"
    kind: Mapped[str] = mapped_column(String(8))
    # "american" | "european"
    style: Mapped[str] = mapped_column(String(12), default="american")
    strike: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    strike_currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Underlying shares per warrant (Bezugsverhältnis): 0.1 = ten warrants per share.
    ratio: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    expiry: Mapped[date] = mapped_column(Date)
    # The underlying's listing as the price store knows it (ANT, EUNL.DE).
    underlying_symbol: Mapped[str] = mapped_column(String(32))
    issuer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class DerivativeQuote(Base):
    """One warrant's price on one day, with what the listed options implied that day."""

    __tablename__ = "derivative_quotes"
    __table_args__ = (UniqueConstraint("isin", "day", name="uq_derivative_quotes_isin_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    isin: Mapped[str] = mapped_column(String(12), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    underlying_symbol: Mapped[str] = mapped_column(String(32))
    # The broker's mid price per warrant, EUR.
    warrant_price_eur: Mapped[float] = mapped_column(Float)
    underlying_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    eur_per_unit: Mapped[float | None] = mapped_column(Float, nullable=True)
    days_to_expiry: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Volatility the warrant's price implies (annual fraction), and the listed
    # options' implied volatility at the nearest strike and expiry, if any.
    warrant_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    listed_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    listed_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    listed_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
