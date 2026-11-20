"""Benchmark comparison for Portfolio -> Performance ("vs benchmark").

The benchmark is the passive core ETF (``passive_core_ticker``, EUNL.DE by
default): the alternative to any action is simply holding more of it, the same
yardstick the prediction ledgers are judged against. The window return is the
change between the last close on or before each end of the window, so a
weekend or holiday on either edge does not shorten it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.foundation import market
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARK = "EUNL.DE"
# A close may predate the window edge by a weekend plus holidays.
MAX_EDGE_GAP_DAYS = 7


def passive_core_symbol(db: Session) -> str:
    """The passive core ETF every comparison is made against."""
    try:
        value = get_public_settings(db).get("passive_core_ticker")
    except Exception:  # pragma: no cover - settings table unavailable
        value = None
    return str(value or DEFAULT_BENCHMARK).upper()


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _last_on_or_before(closes: list[tuple[date, float]], day: date) -> tuple[date, float] | None:
    for d, close in reversed(closes):
        if d <= day:
            return (d, close) if (day - d).days <= MAX_EDGE_GAP_DAYS else None
    return None


def window_return(db: Session, symbol: str, start: date, end: date) -> dict[str, Any]:
    """Return of *symbol* between *start* and *end* as a fraction, or ``None`` when not measurable."""
    result: dict[str, Any] = {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat(), "return": None}
    if end <= start:
        return result
    try:
        rows = market.history(db, symbol, days=(date.today() - start).days + MAX_EDGE_GAP_DAYS + 5)
    except Exception:
        logger.warning("benchmark: price history failed for %s", symbol, exc_info=True)
        return result
    by_day: dict[date, float] = {}
    for row in rows:
        day, close = _as_date(row.get("date")), row.get("close")
        if day is not None and close is not None and float(close) > 0:
            by_day[day] = float(close)
    closes = sorted(by_day.items())
    first = _last_on_or_before(closes, start)
    last = _last_on_or_before(closes, end)
    if first is None or last is None or last[0] <= first[0]:
        return result
    result["return"] = last[1] / first[1] - 1.0
    result["start"], result["end"] = first[0].isoformat(), last[0].isoformat()
    return result


def benchmark_comparison(
    db: Session, *, period_start: date, period_end: date, portfolio_twr: float | None
) -> dict[str, Any]:
    """Portfolio time-weighted return beside the passive core's return over the same window."""
    symbol = passive_core_symbol(db)
    bench = window_return(db, symbol, period_start, period_end)
    bench_return = bench["return"]
    excess = (
        portfolio_twr - bench_return if portfolio_twr is not None and bench_return is not None else None
    )
    days = (period_end - period_start).days
    return {
        "benchmark_symbol": symbol,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "period_days": days,
        "portfolio_twr": portfolio_twr,
        "benchmark_return": bench_return,
        "excess": excess,
        # Under a year the figures are a snapshot of a short stretch, not a track record.
        "short_window": days < 365,
        "message": None if bench_return is not None else f"No {symbol} price history for this window yet.",
    }


__all__ = ["benchmark_comparison", "passive_core_symbol", "window_return"]
