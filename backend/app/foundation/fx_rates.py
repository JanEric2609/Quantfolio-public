"""Dated FX rate provider backed by the fx_rates table (proposal P1).

``fx_cvt`` resolves live spot rates from the provider chain, but valuation needs
the rate that applied on a *given date* so historical snapshots are honest. This
module adds a persistence + resolution layer:

- exact stored rate for (base, quote, date) if present,
- for today, fetch live spot and cache it into the table,
- otherwise carry forward the last known rate on/before the date,
- inverse-pair fallback,
- else None (strict callers then raise).
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import FxRate
from app.foundation.fx_cvt import FxRateUnavailable, get_rate as _spot_rate
from app.foundation.providers.utils import currency_unit

logger = logging.getLogger(__name__)


def _to_date(as_of: date | datetime | None) -> date:
    if as_of is None:
        return datetime.now(UTC).date()
    if isinstance(as_of, datetime):
        return as_of.date()
    return as_of


def upsert_rate(db: Session, base: str, quote: str, on: date, rate: float) -> None:
    """Insert or update the stored rate for (base, quote, on)."""
    base, quote = base.upper(), quote.upper()
    row = (
        db.query(FxRate)
        .filter(FxRate.base == base, FxRate.quote == quote, FxRate.date == on)
        .first()
    )
    if row is None:
        db.add(FxRate(base=base, quote=quote, date=on, rate=Decimal(str(rate))))
    else:
        row.rate = Decimal(str(rate))
    db.commit()


class FxRateProvider:
    """Resolves a conversion rate for a given date, caching provider fetches."""

    @staticmethod
    def get_rate(
        db: Any,
        base: str,
        quote: str,
        as_of: date | datetime | None = None,
    ) -> float | None:
        base = (base or "").upper()
        quote = (quote or "").upper()
        if base == quote:
            return 1.0

        # Without a session we cannot do dated lookups; fall back to live spot.
        if db is None:
            return _spot_rate(base, quote, None)

        target = _to_date(as_of)

        exact = FxRateProvider._exact(db, base, quote, target)
        if exact is not None:
            return exact

        today = datetime.now(UTC).date()
        if target >= today:
            spot = _spot_rate(base, quote, db)
            if spot is not None:
                try:
                    upsert_rate(db, base, quote, today, spot)
                except Exception:  # noqa: BLE001 — caching is best-effort
                    db.rollback()
                    logger.warning("failed to cache fx rate %s->%s", base, quote)
                return spot

        carry = FxRateProvider._carry_forward(db, base, quote, target)
        if carry is not None:
            return carry

        return None

    @staticmethod
    def _exact(db: Session, base: str, quote: str, on: date) -> float | None:
        row = (
            db.query(FxRate)
            .filter(FxRate.base == base, FxRate.quote == quote, FxRate.date == on)
            .first()
        )
        if row is not None:
            return float(row.rate)
        inv = (
            db.query(FxRate)
            .filter(FxRate.base == quote, FxRate.quote == base, FxRate.date == on)
            .first()
        )
        if inv is not None and float(inv.rate) != 0.0:
            return 1.0 / float(inv.rate)
        return None

    @staticmethod
    def _carry_forward(db: Session, base: str, quote: str, on: date) -> float | None:
        row = (
            db.query(FxRate)
            .filter(FxRate.base == base, FxRate.quote == quote, FxRate.date <= on)
            .order_by(FxRate.date.desc())
            .first()
        )
        if row is not None:
            return float(row.rate)
        inv = (
            db.query(FxRate)
            .filter(FxRate.base == quote, FxRate.quote == base, FxRate.date <= on)
            .order_by(FxRate.date.desc())
            .first()
        )
        if inv is not None and float(inv.rate) != 0.0:
            return 1.0 / float(inv.rate)
        return None


def convert(
    amount: float,
    from_currency: str,
    to_currency: str,
    db: Any,
    *,
    strict: bool = False,
    as_of: Any | None = None,
) -> float:
    """Convert an amount from one currency to another.

    When *as_of* (a date/datetime) is given, the rate that applied on that date
    is resolved via the dated ``FxRateProvider`` (table lookup → live spot for
    today → carry-forward), so historical snapshots value at their own date's
    rate. When *as_of* is None the live spot rate is used (unchanged behaviour).

    When no FX rate can be resolved, returning the amount unconverted silently
    mislabels e.g. a USD figure as EUR. That failure is surfaced: a WARNING is
    logged (so it is observable rather than invisible), and callers that must
    not proceed on bad data can pass ``strict=True`` to get an FxRateUnavailable
    instead of a wrong number.
    """
    # Minor units first: upper-casing "GBp" (pence, how the LSE quotes
    # SHEL.L) would read 3,611p as GBP 3,611, a silent 100x.
    from_currency, from_div = currency_unit(from_currency)
    to_currency, to_div = currency_unit(to_currency)
    amount = amount / from_div * to_div
    if from_currency == to_currency:
        return amount

    if as_of is not None:
        rate = FxRateProvider.get_rate(db, from_currency, to_currency, as_of)
    else:
        rate = _spot_rate(from_currency, to_currency, db)
    if rate is None:
        if strict:
            raise FxRateUnavailable(from_currency, to_currency)
        logger.warning(
            "FX rate unavailable for %s->%s; returning amount unconverted (value is NOT in %s)",
            from_currency,
            to_currency,
            to_currency,
        )
        return amount
    return amount * rate
