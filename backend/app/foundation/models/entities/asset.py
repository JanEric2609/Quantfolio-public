from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from decimal import Decimal
from sqlalchemy import Boolean, DateTime, Index, Numeric, String, Text, UniqueConstraint


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("isin", "exchange", "currency", name="uq_assets_isin_exchange_currency"),
        Index("ix_assets_symbol_exchange", "symbol", "exchange"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    exchange: Mapped[str | None] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(240))
    asset_type: Mapped[str] = mapped_column(String(24), default="stock")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    country: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    sector: Mapped[str | None] = mapped_column(String(120))
    industry: Mapped[str | None] = mapped_column(String(160))
    ucits: Mapped[bool] = mapped_column(Boolean, default=False)
    accumulating: Mapped[bool | None] = mapped_column(Boolean)
    distributing: Mapped[bool | None] = mapped_column(Boolean)
    ter: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    provider_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
