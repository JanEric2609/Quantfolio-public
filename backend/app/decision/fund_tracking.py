"""Tracking difference of the curated plan funds against their same-index peers (ADR 0019, amended 2026-10-14).

Monthly, for every curated candidate with a listing: its annualised return
over 3 years from stored closes, minus the median of the curated funds that
track the same index. Every curated fund with a peer accumulates, so the price
return is the total return. A fund without a same-index peer, or without 3
years of closes, gets no figure and scores the neutral 0.5 on that line.

The result is internal state in ``app_settings`` (no catalog entry), refreshed
by the worker's monthly ``index_currency_weights`` job. The plan reads it on
each request; nothing in the request path fetches prices.
"""
from __future__ import annotations

import logging
import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app.decision.monthly_plan_funds import CANDIDATES, FundCandidate

logger = logging.getLogger(__name__)

SETTING_KEY = "fund_peer_td_json"
YEARS = 3
#: The first and last close must lie within this many days of the window's ends.
EDGE_TOLERANCE_DAYS = 10


def annualised_return(rows: Iterable[Mapping[str, Any]], *, today: date, years: int = YEARS) -> float | None:
    """Annualised return in % over the last *years* from ``date``/``close`` rows, or None when they fall short."""
    start = today - timedelta(days=round(365.25 * years))
    points = sorted(
        (d if isinstance(d, date) else date.fromisoformat(str(d)[:10]), float(r["close"]))
        for r in rows
        if (d := r.get("date")) is not None and r.get("close") is not None and float(r["close"]) > 0
    )
    window = [(d, c) for d, c in points if d >= start]
    if not window:
        return None
    (first_day, first), (last_day, last) = window[0], window[-1]
    if (first_day - start).days > EDGE_TOLERANCE_DAYS or (today - last_day).days > EDGE_TOLERANCE_DAYS:
        return None
    span_years = (last_day - first_day).days / 365.25
    return 100.0 * ((last / first) ** (1.0 / span_years) - 1.0)


def peer_differences(returns: Mapping[str, float], index_by_isin: Mapping[str, str]) -> dict[str, float]:
    """Each fund's return minus the median of its index group, for groups of at least two measured funds."""
    groups: dict[str, list[str]] = {}
    for isin in returns:
        groups.setdefault(index_by_isin[isin], []).append(isin)
    out: dict[str, float] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        median = statistics.median(returns[i] for i in members)
        out.update({i: round(returns[i] - median, 4) for i in members})
    return out


def refresh_peer_td(db: Session, *, today: date | None = None,
                    candidates: Iterable[FundCandidate] = CANDIDATES) -> dict[str, Any]:
    """Measure and store every curated fund's peer tracking difference; returns the stored payload."""
    from app.foundation import market
    from app.foundation.settings import upsert_public_settings

    today = today or datetime.now(UTC).date()
    returns: dict[str, float] = {}
    index_by_isin: dict[str, str] = {}
    for c in candidates:
        if not c.ticker:
            continue
        try:
            rows = market.history(db, c.ticker, days=round(365.25 * YEARS) + 30)
        except Exception:
            logger.warning("fund_peer_td: no history for %s", c.ticker, exc_info=True)
            continue
        r = annualised_return(rows, today=today)
        if r is not None:
            returns[c.isin] = r
            index_by_isin[c.isin] = c.index
    payload = {"as_of": today.isoformat(), "years": YEARS, "td_pct": peer_differences(returns, index_by_isin)}
    upsert_public_settings(db, {SETTING_KEY: payload})
    return payload


def stored_peer_td(db: Session) -> dict[str, float]:
    """The last stored tracking differences by ISIN, in % a year; empty before the first run."""
    from app.foundation.settings import get_setting_row

    stored = get_setting_row(db, SETTING_KEY)
    td = stored.get("td_pct") if isinstance(stored, dict) else None
    return {str(k): float(v) for k, v in td.items()} if isinstance(td, dict) else {}

