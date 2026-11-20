"""What the desk can trade on: the four index aliases, ISINs, and tickers resolved from stored tables."""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@dataclass(frozen=True)
class Underlying:
    isin: str
    label: str
    exchange: str  # XETR | XNYS: the calendar of the underlying
    liquid: bool
    is_index: bool = False
    #: A quoted proxy for prices and volatility (an index has no quote): an ETF ISIN on sc, or
    #: an Alpaca symbol for the US indices.
    proxy_isin: str | None = None
    alpaca_symbol: str | None = None


ALIASES: dict[str, Underlying] = {
    "DAX": Underlying("DE0008469008", "DAX", "XETR", True, True, proxy_isin="DE0005933931"),
    "ES50": Underlying("EU0009658145", "Euro Stoxx 50", "XETR", True, True, proxy_isin="IE0008471009"),
    "SPX": Underlying("US78378X1072", "S&P 500", "XNYS", True, True, alpaca_symbol="SPY"),
    "NDX": Underlying("US6311011026", "Nasdaq-100", "XNYS", True, True, alpaca_symbol="QQQ"),
}
LIQUID_ISINS = frozenset(u.isin for u in ALIASES.values())


class UnderlyingUnknown(ValueError):
    """The ticker is ambiguous or not found: the message asks for the ISIN."""


def resolve_underlying(db: Session | None, text: str, user_id: str | None = None) -> Underlying:
    key = text.strip().upper()
    if key in ALIASES:
        return ALIASES[key]
    if ISIN_RE.fullmatch(key):
        known = next((u for u in ALIASES.values() if u.isin == key), None)
        if known:
            return known
        return Underlying(key, key, "XNYS" if key.startswith("US") else "XETR", False)
    candidates: dict[str, str] = {}
    if db is not None:
        from app.foundation.broker_instruments import isin_for_us_ticker
        from app.foundation.live_positions import combined_positions
        from app.foundation.models.entities import IndexUniverseMember

        if user_id:
            for pos in combined_positions(db, user_id):
                if (pos.ticker or "").upper() in (key, key.split(".")[0]) and pos.isin:
                    candidates[pos.isin] = pos.name or key
        for row in db.query(IndexUniverseMember).filter(IndexUniverseMember.symbol.in_([key, f"{key}.DE"])).all():
            candidates[row.isin] = row.name
        us = isin_for_us_ticker(db, key)
        if us:
            candidates[us] = key
    if len(candidates) == 1:
        isin, name = next(iter(candidates.items()))
        return Underlying(isin, name[:60], "XNYS" if isin.startswith("US") else "XETR", isin in LIQUID_ISINS)
    raise UnderlyingUnknown(f"'{text}' is {'ambiguous' if candidates else 'unknown'}: send the ISIN instead.")


def daily_sigma(closes: Sequence[Decimal | float], window: int = 20) -> Decimal | None:
    """Standard deviation of the last *window* daily log returns, or ``None`` with too few closes."""
    values = [float(c) for c in closes if c and float(c) > 0]
    if len(values) < window + 1:
        return None
    rets = [math.log(values[i] / values[i - 1]) for i in range(len(values) - window, len(values))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return Decimal(str(round(math.sqrt(var), 8)))


def last_close_per_day(points: list[tuple]) -> list[Decimal]:
    """One close per calendar day (the last point of each day) from timestamped chart points."""
    by_day: dict[date, Decimal] = {}
    for ts, mid in points:
        by_day[ts.date()] = mid
    return [by_day[d] for d in sorted(by_day)]
