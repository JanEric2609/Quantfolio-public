"""Choosing the product (ADR 0027): search, local filters, quote each candidate, rank by spread."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.foundation.scalable import market
from app.foundation.scalable.cli import ScalableCli

from .limits import LIMITS

MAX_CANDIDATES = 8  # quotes per proposal: the sc session lock is shared with the 6-hourly sync


@dataclass(frozen=True)
class Candidate:
    isin: str
    issuer: str
    product_type: str
    leverage_or_omega: Decimal
    bid: Decimal
    ask: Decimal
    spread_pct: Decimal
    quote_age_s: float
    barrier_distance: Decimal | None = None
    delta: Decimal | None = None
    days_to_expiry: int | None = None
    implied_vol: Decimal | None = None
    realised_vol: Decimal | None = None
    # What the owner would type and see in the broker app.
    name: str = ""  # the issuer's product name from the quote, e.g. "S&P 500 Long 5.812,34 HSBC"
    barrier_or_strike: Decimal | None = None  # knock-out barrier, warrant strike
    expiry: date | None = None  # None: open end

    @property
    def wkn(self) -> str | None:
        return wkn_of(self.isin)

    def as_json(self) -> dict[str, Any]:
        out = {k: (str(v) if isinstance(v, Decimal | date) else v) for k, v in asdict(self).items()}
        out["wkn"] = self.wkn
        return out


def wkn_of(isin: str) -> str | None:
    """The WKN inside a German ISIN (DE + 000 + WKN + check digit); None for any other country."""
    return isin[5:11] if len(isin) == 12 and isin.startswith("DE000") else None


@dataclass(frozen=True)
class Selection:
    candidates: list[Candidate]  # ranked by spread, best first
    dropped: list[dict[str, str]]  # {"isin", "reason"}
    searched: int

    @property
    def chosen(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def strategy_for(direction: str, product_type: str) -> str:
    if product_type == "knockout":
        return "long" if direction == "long" else "short"
    return "call" if direction == "long" else "put"


def spread_fraction(bid: Decimal, ask: Decimal) -> Decimal:
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else Decimal(1)


# Knock-outs are searched highest leverage first (within the cap): the per-trade loss budget then
# buys the most underlying exposure for the same two fees. Sorting by barrier distance would pick
# leverage-1 products whose 3 % minimum stop leaves a fraction of that exposure.
def select_product(
    cli: ScalableCli, underlying_isin: str, direction: str, product_type: str, now: datetime, *,
    sigma: Decimal | None, liquid: bool, clock: Callable[[], datetime] | None = None,
) -> Selection:
    """*clock*: when given, each quote's age is measured when that quote is read (an idea's twenty
    sc calls take time; an age against the time before them reads too young)."""
    strategy = strategy_for(direction, product_type)
    today = now.astimezone(UTC).date()
    if product_type == "knockout":
        listings = market.search_derivatives(
            cli, underlying=underlying_isin, product_type="knockout", strategy=strategy, limit=100,
            leverage_max=str(LIMITS.knockout_max_leverage), sort_field="leverage", sort_order="desc")
    else:
        listings = market.search_derivatives(
            cli, underlying=underlying_isin, product_type="warrant", strategy=strategy, limit=100,
            expiry_from=today + timedelta(days=LIMITS.warrant_min_days_to_expiry),
            sort_field="implied-volatility", sort_order="asc")
    dropped: list[dict[str, str]] = []
    keep: list[market.DerivativeListing] = []
    for row in listings:
        reason = _local_filter(row, product_type, today, sigma)
        if reason:
            dropped.append({"isin": row.isin, "reason": reason})
        elif len(keep) < MAX_CANDIDATES:
            keep.append(row)
    cap = LIMITS.spread_cap_liquid if liquid else LIMITS.spread_cap_other
    out: list[Candidate] = []
    for row in keep:
        quote = market.get_quote(cli, row.isin)
        if quote is None:
            dropped.append({"isin": row.isin, "reason": "no_quote"})
            continue
        read_at = clock() if clock else now
        age = (read_at - quote.ts_utc).total_seconds() if quote.ts_utc else float("inf")
        spread = spread_fraction(quote.bid, quote.ask)
        if quote.outdated or age > LIMITS.max_quote_age_s:
            dropped.append({"isin": row.isin, "reason": "quote_stale"})
        elif spread > cap:
            dropped.append({"isin": row.isin, "reason": "spread_too_wide"})
        else:
            lev = row.leverage if product_type == "knockout" else row.omega
            out.append(Candidate(
                isin=row.isin, issuer=row.issuer, product_type=product_type,
                leverage_or_omega=abs(lev) if lev else Decimal(1), bid=quote.bid, ask=quote.ask,
                spread_pct=spread, quote_age_s=age, barrier_distance=row.distance_to_knockout, delta=row.delta,
                days_to_expiry=(row.expiry - today).days if row.expiry else None,
                implied_vol=row.implied_volatility,
                realised_vol=(sigma * Decimal(252).sqrt()) if (sigma and product_type == "warrant") else None,
                name=quote.name, barrier_or_strike=row.barrier if product_type == "knockout" else row.strike,
                expiry=row.expiry))
    out.sort(key=lambda c: c.spread_pct)
    return Selection(out, dropped, len(listings))


def _local_filter(row: market.DerivativeListing, product_type: str, today, sigma: Decimal | None) -> str | None:
    if product_type == "knockout":
        if row.leverage is None or row.leverage > LIMITS.knockout_max_leverage:
            return "leverage"
        if row.distance_to_knockout is None or sigma is None:
            return "barrier_unknown"
        if abs(row.distance_to_knockout) < LIMITS.knockout_min_barrier_sigmas * sigma:
            return "barrier_too_close"
        return None
    if row.expiry is None or (row.expiry - today).days < LIMITS.warrant_min_days_to_expiry:
        return "near_expiry"
    if row.delta is None or not (LIMITS.warrant_min_abs_delta <= abs(row.delta) <= LIMITS.warrant_max_abs_delta):
        return "delta_band"
    if row.omega is None:
        return "omega_unknown"
    return None
