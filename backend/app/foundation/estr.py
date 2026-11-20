"""Stored €STR fixings and their compounded total-return index (plan 2026-10-12, D3).

A money-market ETF is scored against what it tracks: €STR compounded daily,
not MSCI World (XEON showed -14.2 %/yr "excess" against URTH, audit H5). The
fixings come from the ECB Data Portal (series ``EST.B.EU000A2X2A25.WT``,
percent per year, ACT/360) through the daily risk-free-rate job and are kept
in ``macro_indicators`` (``name="ESTR"``, ``source="ecb"``).
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import logging
from typing import Any, Iterable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ESTR_SERIES_KEY = "EST.B.EU000A2X2A25.WT"
ESTR_NAME = "ESTR"
ESTR_SOURCE = "ecb"
#: History the job backfills once when fewer fixings than this are stored.
BACKFILL_DAYS = 365 * 4 + 30
MIN_STORED_FIXINGS = 500


def store_fixings(db: Session, observations: Iterable[dict[str, Any]]) -> int:
    """Upsert ``[{"date", "value"}]`` ECB observations. Does not commit; returns rows written."""
    from app.foundation.models.entities import MacroIndicator

    written = 0
    for obs in observations:
        raw_day, value = obs.get("date"), obs.get("value")
        if raw_day is None or value is None:
            continue
        try:
            day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
            rate = Decimal(str(float(value)))
        except (TypeError, ValueError):
            continue
        row = (
            db.query(MacroIndicator)
            .filter(MacroIndicator.name == ESTR_NAME, MacroIndicator.date == day,
                    MacroIndicator.source == ESTR_SOURCE)
            .first()
        )
        if row is None:
            db.add(MacroIndicator(name=ESTR_NAME, value=rate, date=day, source=ESTR_SOURCE,
                                  fetched_at=datetime.now(UTC), stale=False))
        else:
            row.value = rate
            row.fetched_at = datetime.now(UTC)
        written += 1
    return written


def stored_fixings(db: Session, start: date | None = None) -> list[tuple[date, float]]:
    """``[(day, rate in % p.a.)]`` ascending."""
    from app.foundation.models.entities import MacroIndicator

    q = db.query(MacroIndicator.date, MacroIndicator.value).filter(
        MacroIndicator.name == ESTR_NAME, MacroIndicator.source == ESTR_SOURCE,
    )
    if start is not None:
        q = q.filter(MacroIndicator.date >= start)
    return [(d, float(v)) for d, v in q.order_by(MacroIndicator.date).all()]


def index_levels(fixings: list[tuple[date, float]], base: float = 100.0) -> dict[str, float]:
    """``{YYYY-MM-DD: level}``: €STR compounded daily, ACT/360.

    The fixing published for a day accrues over the calendar days until the
    next fixing (a Friday rate earns three days).
    """
    levels: dict[str, float] = {}
    if not fixings:
        return levels
    level = base
    levels[fixings[0][0].isoformat()] = level
    for (day, rate), (nxt, _) in zip(fixings, fixings[1:]):
        level *= 1.0 + (rate / 100.0) * (nxt - day).days / 360.0
        levels[nxt.isoformat()] = level
    return levels


def closes_on(db: Session, days: list[str], *, max_carry_days: int = 5) -> dict[str, float]:
    """The €STR index on each requested ``YYYY-MM-DD`` (last fixing at most ``max_carry_days`` old)."""
    if not days:
        return {}
    first = date.fromisoformat(min(days)) - timedelta(days=10)
    levels = index_levels(stored_fixings(db, start=first))
    if not levels:
        return {}
    keys = sorted(levels)
    out: dict[str, float] = {}
    for d in days:
        i = bisect_right(keys, d) - 1
        if i < 0:
            continue
        if (date.fromisoformat(d) - date.fromisoformat(keys[i])).days > max_carry_days:
            continue
        out[d] = levels[keys[i]]
    return out


def refresh_fixings(db: Session, provider: Any) -> dict[str, Any]:
    """Fetch recent fixings (or a 4-year backfill when few are stored) and store them; commits."""
    from app.foundation.models.entities import MacroIndicator

    stored = db.query(MacroIndicator).filter(
        MacroIndicator.name == ESTR_NAME, MacroIndicator.source == ESTR_SOURCE,
    ).count()
    end = datetime.now(UTC).date()
    start = end - timedelta(days=BACKFILL_DAYS if stored < MIN_STORED_FIXINGS else 10)
    result = provider.get_history(ESTR_SERIES_KEY, start=start.isoformat(), end=end.isoformat())
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    observations = result.get("data") or []
    written = store_fixings(db, observations)
    db.commit()
    return {"ok": True, "written": written, "observations": observations}
