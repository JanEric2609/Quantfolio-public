"""Unified valuation service (proposal P2).

One place where ``quantity × price → converted total`` is computed. Every money
path (snapshots, wealth API, paper, competition, ``holding_market_value``)
delegates here so the currency-conversion and stale-quote rules cannot drift
apart across duplicated copies.

Conversion is done through :mod:`app.foundation.fx_cvt` (which routes dated
lookups through the P1 ``FxRateProvider`` when ``as_of`` is given, and the live
spot chain otherwise).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from sqlalchemy.orm import Session

from app.foundation.models.entities import Holding, PriceCache
from app.foundation.data_backbone.listing_currency import resolve_quote_currency
from app.foundation.fx_rates import convert as fx_convert


@dataclass(frozen=True)
class ValuedHolding:
    """The valuation of a single holding in a reporting currency."""

    ticker: str | None
    quantity: float
    price: float
    source_currency: str
    reporting_ccy: str
    price_date: date | None
    is_stale: bool
    price_source: str  # "market" | "cost_basis" | "unknown_cost"
    converted_value: float


@dataclass(frozen=True)
class ValuedPortfolio:
    """The valuation of a set of holdings folded into a reporting currency total."""

    holdings: list[ValuedHolding]
    reporting_ccy: str
    total_value: float


def value_holding(
    db: Session,
    holding: Holding,
    *,
    as_of: date | datetime | None = None,
    reporting_ccy: str = "EUR",
) -> ValuedHolding:
    """Value one holding in ``reporting_ccy``.

    Uses the latest cached market price (converted from its own quote currency),
    falling back to cost basis (the holding's own currency) when no price is
    available. Staleness is flagged from the ``PriceCache`` row, and always for
    the cost-basis fallback (there is no live market mark).
    """
    reporting_ccy = (reporting_ccy or "EUR").upper()
    quantity = float(holding.quantity)

    if holding.ticker:
        cached = (
            db.query(PriceCache)
            .filter(PriceCache.ticker == holding.ticker.upper())
            .order_by(PriceCache.date.desc())
            .first()
        )
        if cached is not None:
            price = float(cached.close)
            # The resolved quote unit, not the cached label: history rows
            # used to be cached as "EUR" whatever the listing (AAPL, SHEL.L).
            # The cached label stays the hint until the listing is audited.
            # Case is kept: "GBp" is pence (fx_rates.convert divides by 100).
            source_ccy = resolve_quote_currency(db, holding.ticker, hint=cached.currency)
            raw = quantity * price
            converted = fx_convert(raw, source_ccy, reporting_ccy, db, as_of=as_of)
            return ValuedHolding(
                ticker=holding.ticker,
                quantity=quantity,
                price=price,
                source_currency=source_ccy,
                reporting_ccy=reporting_ccy,
                price_date=cached.date,
                is_stale=bool(cached.stale),
                price_source="market",
                converted_value=converted,
            )

    # Cost-basis fallback (assume the holding's own currency).
    cost_ccy = (holding.currency or "EUR").upper()
    if holding.avg_buy_price is None:
        return ValuedHolding(
            ticker=holding.ticker,
            quantity=quantity,
            price=0.0,
            source_currency=cost_ccy,
            reporting_ccy=reporting_ccy,
            price_date=None,
            is_stale=True,
            price_source="unknown_cost",
            converted_value=0.0,
        )
    price = float(holding.avg_buy_price)
    raw = quantity * price
    converted = fx_convert(raw, cost_ccy, reporting_ccy, db, as_of=as_of)
    return ValuedHolding(
        ticker=holding.ticker,
        quantity=quantity,
        price=price,
        source_currency=cost_ccy,
        reporting_ccy=reporting_ccy,
        price_date=None,
        is_stale=True,
        price_source="cost_basis",
        converted_value=converted,
    )


def value_portfolio(
    db: Session,
    holdings: Iterable[Holding],
    *,
    as_of: date | datetime | None = None,
    reporting_ccy: str = "EUR",
) -> ValuedPortfolio:
    """Fold a set of holdings into a single ``reporting_ccy`` total."""
    reporting_ccy = (reporting_ccy or "EUR").upper()
    valued = [
        value_holding(db, h, as_of=as_of, reporting_ccy=reporting_ccy) for h in holdings
    ]
    total = sum(v.converted_value for v in valued)
    return ValuedPortfolio(holdings=valued, reporting_ccy=reporting_ccy, total_value=total)
