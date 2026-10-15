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

import bisect
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
#: A stored measurement older than this is not used (the job runs monthly).
MAX_AGE_DAYS = 62

Points = list[tuple[date, float]]


def _points(rows: Iterable[Mapping[str, Any]]) -> Points:
    return sorted(
        (d if isinstance(d, date) else date.fromisoformat(str(d)[:10]), float(r["close"]))
        for r in rows
        if (d := r.get("date")) is not None and r.get("close") is not None and float(r["close"]) > 0
    )


def _window(points: Points, *, today: date, years: int) -> Points | None:
    """The closes inside the window, or None when they do not reach both of its ends."""
    start = today - timedelta(days=round(365.25 * years))
    window = [(d, c) for d, c in points if d >= start]
    if not window:
        return None
    if (window[0][0] - start).days > EDGE_TOLERANCE_DAYS or (today - window[-1][0]).days > EDGE_TOLERANCE_DAYS:
        return None
    return window


def _close_on_or_before(points: Points, day: date) -> float | None:
    i = bisect.bisect_right([d for d, _ in points], day)
    return points[i - 1][1] if i else None


def annualised_return(rows: Iterable[Mapping[str, Any]], *, today: date, years: int = YEARS) -> float | None:
    """Annualised return in % over the last *years* from ``date``/``close`` rows, or None when they fall short."""
    window = _window(_points(rows), today=today, years=years)
    if window is None:
        return None
    (first_day, first), (last_day, last) = window[0], window[-1]
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


def aligned_returns(points_by_isin: Mapping[str, Points], index_by_isin: Mapping[str, str], *,
                    today: date, years: int = YEARS) -> dict[str, float]:
    """Annualised returns in %, measured between the same two dates within each index group.

    A group's end is its earliest last close and its start the latest first
    close, so two funds of one index are never compared across a market move
    that only one of them has priced yet. A group needs two measurable funds.
    """
    groups: dict[str, dict[str, Points]] = {}
    for isin, points in points_by_isin.items():
        window = _window(points, today=today, years=years)
        if window is not None:
            groups.setdefault(index_by_isin[isin], {})[isin] = window
    out: dict[str, float] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        end = min(w[-1][0] for w in members.values())
        start = max(w[0][0] for w in members.values())
        span_years = (end - start).days / 365.25
        if span_years <= 0:
            continue
        for isin, window in members.items():
            first, last = _close_on_or_before(window, start), _close_on_or_before(window, end)
            if first and last:
                out[isin] = 100.0 * ((last / first) ** (1.0 / span_years) - 1.0)
    return out


def refresh_peer_td(db: Session, *, today: date | None = None,
                    candidates: Iterable[FundCandidate] = CANDIDATES) -> dict[str, Any]:
    """Measure and store every curated fund's peer tracking difference; returns the stored payload."""
    from app.foundation import market
    from app.foundation.settings import upsert_public_settings

    today = today or datetime.now(UTC).date()
    points: dict[str, Points] = {}
    index_by_isin: dict[str, str] = {}
    for c in candidates:
        if not c.ticker:
            continue
        try:
            rows = market.history(db, c.ticker, days=round(365.25 * YEARS) + 30)
        except Exception:
            logger.warning("fund_peer_td: no history for %s", c.ticker, exc_info=True)
            continue
        points[c.isin] = _points(rows)
        index_by_isin[c.isin] = c.index
    td = peer_differences(aligned_returns(points, index_by_isin, today=today), index_by_isin)
    payload = {"as_of": today.isoformat(), "years": YEARS, "td_pct": td}
    if not td:
        # A provider outage measures nothing; keep the last good figures (they age out by MAX_AGE_DAYS).
        logger.warning("fund_peer_td: nothing measured on %s; the stored figures stay", today)
        return payload
    upsert_public_settings(db, {SETTING_KEY: payload})
    return payload


def stored_peer_td(db: Session, *, today: date | None = None) -> dict[str, float]:
    """The last stored tracking differences by ISIN, in % a year.

    Empty before the first run and once the measurement is older than
    :data:`MAX_AGE_DAYS`, so a stale figure scores the neutral 0.5.
    """
    from app.foundation.settings import get_setting_row

    stored = get_setting_row(db, SETTING_KEY)
    if not isinstance(stored, dict):
        return {}
    try:
        as_of = date.fromisoformat(str(stored.get("as_of"))[:10])
    except ValueError:
        return {}
    if ((today or datetime.now(UTC).date()) - as_of).days > MAX_AGE_DAYS:
        return {}
    td = stored.get("td_pct")
    return {str(k): float(v) for k, v in td.items()} if isinstance(td, dict) else {}



#: A held fund trailing its best same-index peer by more than this (% a year) is flagged.
GAP_FLAG_PCT = 0.10


def held_funds(db: Session, user_id: str, *, today: date | None = None) -> dict[str, Any]:
    """The Trust page's "funds" row: each held curated fund's tracking difference against its peers.

    Descriptive, monthly (the figures come from :func:`refresh_peer_td`). A
    fund outside the curated list has no same-index peers here and is only
    counted. ``gap_pct`` is how far the fund trails the best measured peer on
    its index, in % a year; above :data:`GAP_FLAG_PCT` it is flagged.
    """
    from app.foundation.live_positions import combined_positions
    from app.foundation.settings import get_setting_row

    stored = get_setting_row(db, SETTING_KEY)
    as_of = stored.get("as_of") if isinstance(stored, dict) else None
    td = stored_peer_td(db, today=today)
    by_isin = {c.isin: c for c in CANDIDATES}
    funds: list[dict[str, Any]] = []
    others = 0
    for pos in combined_positions(db, user_id, include_unreconciled=True):
        c = by_isin.get(pos.isin)
        if c is None:
            others += 1
            continue
        own = td.get(c.isin)
        peers = [p for p in CANDIDATES if p.index == c.index and p.isin != c.isin and p.isin in td]
        best = max(peers, key=lambda p: td[p.isin], default=None)
        gap = round(td[best.isin] - own, 4) if best is not None and own is not None else None
        funds.append({
            "isin": c.isin,
            "name": c.name,
            "index": c.index,
            "value": float(pos.value or 0),
            "td_pct": own,
            "best_peer": (
                {"isin": best.isin, "name": best.name, "td_pct": td[best.isin]}
                if best is not None and gap is not None and gap > 0 else None
            ),
            "gap_pct": max(gap, 0.0) if gap is not None else None,
            "flag": gap is not None and gap > GAP_FLAG_PCT,
        })
    funds.sort(key=lambda f: -f["value"])
    return {
        "as_of": as_of,
        "years": YEARS,
        "flag_pct": GAP_FLAG_PCT,
        "funds": funds,
        "n_other_positions": others,
        "note": (
            "Tracking difference: a fund's yearly return over 3 years minus the median of the curated funds on the "
            "same index, from stored closes. Measured monthly; a gap is a reason to look, not to switch: a switch "
            "realises gains and costs a spread."
        ),
    }
