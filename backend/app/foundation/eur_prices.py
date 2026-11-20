"""Daily closes restated in EUR, for every analysis that compares or combines listings.

The book is reported in EUR, but its lines trade in EUR (EUNL.DE), USD
(VWRA.L, AMC) and pence (an LSE equity). Combining native closes treats a
dollar as a euro and drops the currency return an investor in EUR actually
earns: ``R_EUR = (1 + R_local)(1 + R_FX) - 1``. Every close is therefore
multiplied by the EUR value of one unit of its quote currency on the same
day, before any return is taken.

FX comes from the ``USD<ccy>=X`` bars Discover already ingests
(:func:`market.usd_per_unit_by_date`): EUR per unit of ``ccy`` is
``usd_per_unit(ccy) / usd_per_unit(EUR)``. A day without a rate uses the last
rate at most :data:`MAX_FX_CARRY_DAYS` calendar days old (weekends, a missed
ingest); older than that, the close is dropped rather than priced at a stale
rate.
"""
from __future__ import annotations

import logging
from bisect import bisect_right
from datetime import date

from sqlalchemy.orm import Session

from app.foundation import market as market_service
from app.foundation.data_backbone.listing_currency import resolve_quote_currency
from app.foundation.providers.utils import currency_unit

logger = logging.getLogger(__name__)

MAX_FX_CARRY_DAYS = 5

# MSCI World, net total return, in EUR, unhedged: iShares Core MSCI World on
# Xetra (accumulating, so its price is a total-return series). GIPS asks for a
# benchmark in the portfolio's currency that matches its mandate.
DEFAULT_BENCHMARK = "EUNL.DE"


def eur_per_unit_by_date(
    db: Session, currency: str, days: int = 365 * 3 + 30, *, allow_live: bool = False
) -> dict[str, float] | None:
    """``{YYYY-MM-DD: EUR per 1 unit of currency}``; ``None`` when unavailable."""
    ccy = currency.upper()
    if ccy == "EUR":
        return None
    usd_per_eur = market_service.usd_per_unit_by_date(db, "EUR", days=days, allow_live=allow_live)
    if not usd_per_eur:
        return None
    if ccy == "USD":
        return {d: 1.0 / r for d, r in usd_per_eur.items() if r > 0}
    usd_per_ccy = market_service.usd_per_unit_by_date(db, ccy, days=days, allow_live=allow_live)
    if not usd_per_ccy:
        return None
    return {d: usd_per_ccy[d] / usd_per_eur[d] for d in usd_per_ccy.keys() & usd_per_eur.keys() if usd_per_eur[d] > 0}


def _as_of(rates: dict[str, float]):
    keys = sorted(rates)

    def lookup(day: str) -> float | None:
        i = bisect_right(keys, day) - 1
        if i < 0:
            return None
        if (date.fromisoformat(day) - date.fromisoformat(keys[i])).days > MAX_FX_CARRY_DAYS:
            return None
        return rates[keys[i]]

    return lookup


def to_eur(
    db: Session,
    symbol: str,
    closes: dict[str, float],
    *,
    days: int = 365 * 3 + 30,
    rate_cache: dict[str, dict[str, float] | None] | None = None,
) -> tuple[dict[str, float], str]:
    """Restate ``{date: close}`` of ``symbol`` in EUR: (closes in EUR, ISO currency).

    Returns ``({}, ccy)`` when the quote currency is not EUR and no rate is
    available: an unconverted series would be silently wrong. ``rate_cache``
    keeps one rate series per currency across calls.
    """
    ccy, divisor = currency_unit(resolve_quote_currency(db, symbol))
    scaled = {d: c / divisor for d, c in closes.items()}
    if ccy == "EUR" or not scaled:
        return scaled, "EUR"
    if rate_cache is not None and ccy in rate_cache:
        rates = rate_cache[ccy]
    else:
        rates = eur_per_unit_by_date(db, ccy, days=days)
        if rate_cache is not None:
            rate_cache[ccy] = rates
    if not rates:
        logger.warning("to_eur: no %s->EUR rate for %s; series dropped", ccy, symbol)
        return {}, ccy
    rate_on = _as_of(rates)
    out: dict[str, float] = {}
    for d, close in scaled.items():
        rate = rate_on(d)
        if rate is not None:
            out[d] = close * rate
    return out, ccy


def eur_price(
    db: Session,
    symbol: str,
    price: float,
    on: str | None = None,
    *,
    rate_cache: dict[str, dict[str, float] | None] | None = None,
) -> float | None:
    """One price of ``symbol`` in its quote currency, restated in EUR on ``on`` (default today).

    ``None`` when no rate at most :data:`MAX_FX_CARRY_DAYS` old exists.
    """
    day = on or date.today().isoformat()
    days = max(40, (date.today() - date.fromisoformat(day)).days + 40)
    out, _ccy = to_eur(db, symbol, {day: float(price)}, days=days, rate_cache=rate_cache)
    return out.get(day)


def eur_closes(
    db: Session,
    symbol: str,
    days: int = 730,
    *,
    allow_live: bool = True,
    rate_cache: dict[str, dict[str, float] | None] | None = None,
) -> dict[str, float]:
    """``{YYYY-MM-DD: close in EUR}`` over the last ``days`` calendar days."""
    try:
        bars = market_service.history(db, symbol, days=days, allow_live=allow_live)
    except Exception as exc:  # noqa: BLE001 - a provider outage means no series
        logger.warning("eur_closes: history for %s failed: %s", symbol, exc)
        return {}
    closes: dict[str, float] = {}
    for row in bars or []:
        close = row.get("close")
        if close is None:
            continue
        value = float(close)
        if value > 0:
            closes[str(row.get("date"))[:10]] = value
    return to_eur(db, symbol, closes, days=days + 30, rate_cache=rate_cache)[0]


def returns_by_date(closes: dict[str, float]) -> dict[str, float]:
    """Close-to-close simple returns keyed by the later date."""
    keys = sorted(closes)
    return {
        keys[i]: closes[keys[i]] / closes[keys[i - 1]] - 1.0
        for i in range(1, len(keys))
        if closes[keys[i - 1]] > 0
    }


def benchmark_ticker(db: Session) -> str:
    """The configured benchmark (Control Center), MSCI World EUR by default."""
    from app.foundation.settings import get_public_settings

    value = str(get_public_settings(db).get("benchmark_ticker") or "").strip().upper()
    return value or DEFAULT_BENCHMARK


def benchmark_returns_eur(
    db: Session, ticker: str | None = None, days: int = 730, *, allow_live: bool = True
) -> tuple[str, dict[str, float]]:
    """(ticker, ``{date: EUR return}``) of the benchmark."""
    symbol = (ticker or benchmark_ticker(db)).upper()
    return symbol, returns_by_date(eur_closes(db, symbol, days=days, allow_live=allow_live))
