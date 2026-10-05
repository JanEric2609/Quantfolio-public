from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, Numeric, String, Text, UniqueConstraint


class PriceCache(Base):
    __tablename__ = "price_cache"
    __table_args__ = (UniqueConstraint("ticker", "date", name="uq_price_cache_ticker_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")


class ListingCurrency(Base):
    """The unit a symbol's stored prices are quoted in, as its provider reports it.

    ``bar_prices.currency`` and ``price_cache.currency`` were once filled
    from the ``assets`` table (a fund's base currency: EUNL.DE read USD) or an
    "EUR" fallback, so 429 of 780 symbols were mislabelled (2026-09-28). This
    table is what price readers consult first
    (``data_backbone.listing_currency.resolve_quote_currency``). ``currency``
    keeps the provider's case: "GBp" is pence, "GBP" pounds.
    """

    __tablename__ = "listing_currencies"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(40))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class IndexCurrencyWeights(Base):
    """The currencies an equity index's constituents trade in, as weights.

    What an unhedged index ETF is exposed to: EUNL.DE is quoted in EUR on
    Xetra, but ~73% of MSCI World trades in USD. Refreshed from a tracking
    fund's published holdings (``foundation.etf_currency``); the module's
    seed values stand in until the first refresh. The same refresh stores
    the constituents' country weights (``country_weights_json``).
    """

    __tablename__ = "index_currency_weights"

    index_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    weights_json: Mapped[dict] = mapped_column(JSON)
    #: Country -> weight from the same holdings file (the ETF page's
    #: geographic breakdown); null for rows refreshed before 0120.
    country_weights_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    as_of: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(200))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class AnalystEstimateSnapshot(Base):
    """One day's analyst consensus EPS for a symbol's current fiscal year.

    Yahoo's EPS trend: today's consensus and what it was 7, 30, 60 and 90
    days earlier. The 30-day change is Discover's live estimate-revision
    signal (``data_backbone.analyst_estimates``); the stored days are the
    point-in-time history the WRDS IBES extract no longer provides.
    """

    __tablename__ = "analyst_estimate_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "snapshot_date", "period", name="uq_analyst_estimate_snapshot"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    period: Mapped[str] = mapped_column(String(8), default="0y")
    eps_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_7d_ago: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_30d_ago: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_60d_ago: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_90d_ago: Mapped[float | None] = mapped_column(Float, nullable=True)
    analysts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="yfinance_eps_trend")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Fundamental(Base):
    __tablename__ = "fundamentals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


class MacroIndicator(Base):
    __tablename__ = "macro_indicators"
    __table_args__ = (UniqueConstraint("name", "date", "source", name="uq_macro_name_date_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    name: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(40))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


class DataQualityEvent(Base):
    __tablename__ = "data_quality_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    severity: Mapped[str] = mapped_column(String(24), default="warning")
    message: Mapped[str] = mapped_column(Text)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
