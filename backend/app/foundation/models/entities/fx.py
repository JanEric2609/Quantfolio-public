from datetime import date as _date
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class FxRate(Base):
    """Daily FX reference rate for currency conversion (proposal P1).

    Keyed on (base, quote, date). Rates are resolved from the provider chain and
    cached here so historical snapshots can be valued at the rate that applied on
    their own date, and so repeated conversions don't re-hit providers.
    """

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base", "quote", "date", name="uq_fx_rates_base_quote_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    base: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    quote: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    date: Mapped[_date] = mapped_column(Date, nullable=False, index=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
