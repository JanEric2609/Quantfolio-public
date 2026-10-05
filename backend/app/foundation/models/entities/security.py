"""Security master entities (stage 1 of DEC-B, remediation task T2.1).

``Security`` is the canonical instrument record keyed by ISIN (natural key).
``SecurityListing`` carries per-venue trading data (ISO 10383 MIC + symbol) so
a venue such as XETRA can be canonical per DEC-A. ``SecurityAlias`` is the
permanent resolution cache keyed by sha256(normalized alias)+source: once an
alias has been resolved to a security it is stored forever, so external
identifier lookups become once-per-instrument-ever.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk


class Security(Base):
    """Canonical instrument master row; ISIN is the natural key."""

    __tablename__ = "securities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    isin: Mapped[str] = mapped_column(String(12), nullable=False, unique=True, index=True)
    canonical_name: Mapped[str] = mapped_column(String(240), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(24), nullable=False, default="stock")
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    ucits: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    domicile_country: Mapped[str | None] = mapped_column(String(80))
    ter: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    provider_meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class SecurityListing(Base):
    """Per-venue trading data for a security (MIC + venue symbol)."""

    __tablename__ = "security_listings"
    __table_args__ = (UniqueConstraint("mic", "symbol", name="uq_security_listings_mic_symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    mic: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange_label: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SecurityAlias(Base):
    """Permanent alias→security cache keyed by sha256(normalized alias)+source.

    ``alias_value`` keeps the raw original string for forensics while lookups
    go through the hashed key, so the same wire alias never triggers a second
    external resolution.
    """

    __tablename__ = "security_aliases"
    __table_args__ = (
        UniqueConstraint("source", "alias_sha256", name="uq_security_aliases_source_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    alias_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    alias_value: Mapped[str] = mapped_column(Text, nullable=False)
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
