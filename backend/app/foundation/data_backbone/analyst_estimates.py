"""Analyst consensus EPS snapshots and the live estimate-revision signal.

Discover's ``estimate_revision`` signal read a WRDS IBES extract that is
exported by hand; past its 92-day staleness limit the signal is empty. Owner
decision 2026-09-29: keep IBES for when a fresh export exists, and build a
live source that needs no WRDS.

Yahoo's EPS trend gives, for the current fiscal year, the consensus today
and 7, 30, 60 and 90 days ago; it covers the European names too (SAP.DE 23
analysts, ASML.AS 31, BBVA.MC 15 on 2026-09-29). :func:`live_revision` is
the 30-day change, ``(current - 30d ago) / |30d ago|``: the same quantity as
IBES's monthly ``revision_momentum`` for FPI 1, available from the first
snapshot. Every snapshot is stored (``analyst_estimate_snapshots``) so the
point-in-time history accumulates for backtests.

Guards:

* Fewer than :data:`MIN_ANALYSTS` estimates is not a consensus.
* A move over :data:`MAX_PLAUSIBLE_REVISION` in 30 days is read as a fiscal
  year roll, where the "30 days ago" figure belongs to the previous year
  (the trap that turned LRCX's year-end into a "+65% revision" in IBES), and
  gives no signal.
* There is no dispersion, so no SUE from this source.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import AnalystEstimateSnapshot

logger = logging.getLogger(__name__)

SOURCE = "yfinance_eps_trend"
MIN_ANALYSTS = 3
MAX_PLAUSIBLE_REVISION = 0.5
#: A snapshot this recent is reused instead of fetching again.
REUSE_WITHIN = timedelta(days=3)

_TREND_COLUMNS = {
    "current": "eps_current",
    "7daysAgo": "eps_7d_ago",
    "30daysAgo": "eps_30d_ago",
    "60daysAgo": "eps_60d_ago",
    "90daysAgo": "eps_90d_ago",
}


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN -> None


def fetch_eps_trend(symbol: str) -> dict[str, Any] | None:
    """Yahoo's current-fiscal-year EPS trend and analyst count for *symbol*."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    trend = ticker.eps_trend
    if trend is None or getattr(trend, "empty", True) or "0y" not in trend.index:
        return None
    row = trend.loc["0y"]
    out: dict[str, Any] = {column: _float(row.get(key)) for key, column in _TREND_COLUMNS.items()}
    currency = row.get("currency")
    out["currency"] = str(currency)[:3] if isinstance(currency, str) and currency else None
    try:
        estimate = ticker.earnings_estimate
        analysts = _float(estimate.loc["0y", "numberOfAnalysts"]) if estimate is not None and "0y" in estimate.index else None
    except Exception:  # noqa: BLE001 - the count is optional
        analysts = None
    out["analysts"] = int(analysts) if analysts is not None else None
    return out if out["eps_current"] is not None else None


def snapshot(
    db: Session,
    symbol: str,
    *,
    fetch: Callable[[str], dict[str, Any] | None] | None = None,
    today: date | None = None,
) -> AnalystEstimateSnapshot | None:
    """The snapshot for *symbol*: a stored one from the last few days, else a
    new one fetched and stored. None when Yahoo has no EPS trend. Commits."""
    sym = symbol.strip().upper()
    day = today or datetime.now(UTC).date()
    recent = (
        db.query(AnalystEstimateSnapshot)
        .filter(
            AnalystEstimateSnapshot.symbol == sym,
            AnalystEstimateSnapshot.period == "0y",
            AnalystEstimateSnapshot.snapshot_date >= day - REUSE_WITHIN,
            AnalystEstimateSnapshot.snapshot_date <= day,
        )
        .order_by(AnalystEstimateSnapshot.snapshot_date.desc())
        .first()
    )
    if recent is not None:
        return recent
    try:
        values = (fetch or fetch_eps_trend)(sym)
    except Exception as exc:  # noqa: BLE001 - a provider failure is "no data"
        logger.info("analyst_estimates: %s fetch failed: %s", sym, exc)
        return None
    if not values:
        return None
    row = AnalystEstimateSnapshot(
        symbol=sym, snapshot_date=day, period="0y", source=SOURCE, fetched_at=datetime.now(UTC),
        **{k: values.get(k) for k in (*_TREND_COLUMNS.values(), "analysts", "currency")},
    )
    db.add(row)
    db.commit()
    return row


def revision_from(row: AnalystEstimateSnapshot | None) -> dict[str, Any] | None:
    """The 30-day consensus revision of one snapshot, or None when it is not
    a usable signal (see module docstring)."""
    if row is None or row.eps_current is None or not row.eps_30d_ago:
        return None
    if row.analysts is not None and row.analysts < MIN_ANALYSTS:
        return None
    revision = (row.eps_current - row.eps_30d_ago) / abs(row.eps_30d_ago)
    if abs(revision) > MAX_PLAUSIBLE_REVISION:
        return None
    return {
        "revision_momentum": revision,
        "analysts": row.analysts,
        "snapshot_date": row.snapshot_date.isoformat(),
        "source": row.source,
    }


def live_revision(
    db: Session,
    symbol: str,
    *,
    fetch: Callable[[str], dict[str, Any] | None] | None = None,
    today: date | None = None,
) -> dict[str, Any] | None:
    """:func:`revision_from` of the current snapshot of *symbol*."""
    return revision_from(snapshot(db, symbol, fetch=fetch, today=today))


def snapshot_many(
    db: Session,
    symbols: Iterable[str],
    *,
    fetch: Callable[[str], dict[str, Any] | None] | None = None,
    today: date | None = None,
) -> dict[str, int]:
    """Snapshot each symbol (the nightly job). Returns counts."""
    counts = {"stored_or_recent": 0, "no_data": 0}
    for sym in sorted({s.strip().upper() for s in symbols if s and s.strip()}):
        row = snapshot(db, sym, fetch=fetch, today=today)
        counts["stored_or_recent" if row is not None else "no_data"] += 1
    logger.info("analyst_estimates snapshot: %s", counts)
    return counts
