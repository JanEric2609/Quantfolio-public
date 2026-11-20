"""EUR close series and the trading calendar for the evidence ledgers (ADR 0018 §3).

One loader per batch: each symbol is read once, restated in EUR through
``foundation.eur_prices`` and kept as a sorted ``(date, close)`` list. The
calendar is the passive core's trading days. Prices are carried forward across
days without a close (:meth:`EurSeries.price_on`), which is what turns a
stock that stopped trading into cash for the rest of its window.
"""
from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Callable
from datetime import date

from sqlalchemy.orm import Session

from app.foundation import eur_prices, market

logger = logging.getLogger(__name__)

Series = list[tuple[date, float]]
#: ``(db, symbol, days, refresh) -> {YYYY-MM-DD: EUR close}``; tests inject one.
Loader = Callable[[Session, str, int, bool], dict[str, float]]


def load_eur_closes(db: Session, symbol: str, days: int, refresh: bool) -> dict[str, float]:
    """EUR closes of *symbol* over *days* calendar days; catches stale bars up when *refresh*."""
    if refresh:
        try:
            market.refresh_stale_bars(db, symbol, max_age_days=1.5)
        except Exception as exc:  # noqa: BLE001 - a failed catch-up leaves the stored bars
            logger.warning("ledger_prices: catch-up for %s failed: %s", symbol, exc)
    return eur_prices.eur_closes(db, symbol, days=days, allow_live=refresh)


class EurSeries:
    """Per-batch cache of EUR close series."""

    def __init__(self, db: Session, *, days: int, refresh: bool, loader: Loader | None = None):
        self._db, self._days, self._refresh = db, max(int(days), 30), refresh
        self._loader = loader or load_eur_closes
        self._cache: dict[str, Series] = {}
        self._dates: dict[str, list[date]] = {}

    def series(self, symbol: str) -> Series:
        key = symbol.upper()
        if key not in self._cache:
            try:
                raw = self._loader(self._db, key, self._days, self._refresh)
            except Exception as exc:  # noqa: BLE001 - no series means "no price", never a crash
                logger.warning("ledger_prices: %s unavailable: %s", key, exc)
                raw = {}
            points: dict[date, float] = {}
            for day, close in (raw or {}).items():
                try:
                    d, c = date.fromisoformat(str(day)[:10]), float(close)
                except (TypeError, ValueError):
                    continue
                if c > 0:
                    points[d] = c
            self._cache[key] = sorted(points.items())
            self._dates[key] = [d for d, _ in self._cache[key]]
        return self._cache[key]

    def seed(self, symbol: str, series: Series) -> None:
        """Reuse a series another loader already read (the benchmark)."""
        key = symbol.upper()
        self._cache[key] = list(series)
        self._dates[key] = [d for d, _ in series]

    def last_date(self, symbol: str) -> date | None:
        s = self.series(symbol)
        return s[-1][0] if s else None

    def price_on(self, symbol: str, day: date) -> tuple[float, date] | None:
        """Last EUR close on or before *day*, carried forward without limit."""
        s = self.series(symbol)
        i = bisect_right(self._dates[symbol.upper()], day) - 1
        return (s[i][1], s[i][0]) if i >= 0 else None

    def calendar(self, symbol: str) -> list[date]:
        """Trading days of *symbol* (the benchmark), oldest first."""
        self.series(symbol)
        return list(self._dates[symbol.upper()])


def daily_return(prices: EurSeries, symbol: str, prev: date, day: date) -> tuple[float, bool]:
    """``(return from prev to day, fresh)``; ``(0.0, False)`` without a price at *prev*.

    *fresh* is whether the symbol has a close dated exactly *day*. A missing
    close carries the last one forward (own return 0), and the move lands on
    the next close instead.
    """
    a = prices.price_on(symbol, prev)
    b = prices.price_on(symbol, day)
    if a is None or b is None:
        return 0.0, False
    return b[0] / a[0] - 1.0, b[1] == day


def window_days(calendar: list[date], issue_day: date, horizon: int) -> list[date]:
    """The *horizon* trading days a call issued on *issue_day* is open (entry at that day's close)."""
    start = bisect_right(calendar, issue_day)
    return calendar[start : start + int(horizon)]
