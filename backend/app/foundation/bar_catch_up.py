"""Catch up daily bar series that miss their exchange's last closed session.

Check-in 2026-11-19: at 01:24 UTC Yahoo answered the nightly prefetch without
the 2026-11-18 bar for nearly every European listing (Xetra 2 of 91, London 4
of 78) and half the US ones. Ingestion rejected the row that came back with
a NaN close, stored nothing new, and still reported success, so nothing
asked again: paper sleeves, Discover's stages and the advisor ran on
2026-11-17 closes. Yahoo had the bars a few hours later.

:func:`catch_up_lagging_bars` asks again for every series that should hold a
newer bar than it does:

* a series is *maintained* when its newest bar is at most
  :data:`MAINTAINED_DAYS` old, and every symbol a paper sleeve holds counts
  whatever its age (EIMI.L sat at 2026-11-13: it is in no universe the
  nightly prefetch walks);
* it *lags* when its exchange has closed a session after its newest bar, at
  least :data:`GRACE` ago. Exchange holidays come from the same calendars
  the bar guard uses (``bar_guard.calendar_code``), so a US holiday does
  not refetch every US series; a symbol without a known calendar expects
  the last weekday.

Quarantined series are left to their own repair path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: A series whose newest bar is older than this is not maintained daily (a
#: delisted name, a one-off lookup) and is not chased, unless a paper sleeve
#: holds it.
MAINTAINED_DAYS = 10
#: How long after a session's close its bar is expected from the provider.
GRACE = pd.Timedelta(hours=3)
#: How far back to look for the last closed session.
_LOOKBACK_DAYS = 10


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def last_closed_session(symbol: str, now: datetime | None = None) -> date:
    """The newest session of *symbol*'s exchange that closed at least :data:`GRACE` before *now*.

    Without a known calendar (indices, FX, unknown suffixes): the last
    weekday before today (UTC).
    """
    from app.foundation.data_backbone.bar_guard import _calendar, calendar_code

    moment = now or datetime.now(UTC)
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    now_ts = pd.Timestamp(moment)
    today_date = moment.date()
    code = calendar_code(symbol)
    if code is not None:
        try:
            cal = _calendar(code)
            for back in range(_LOOKBACK_DAYS + 1):
                d = today_date - timedelta(days=back)
                day = pd.Timestamp(d)
                if day < cal.first_session or day > cal.last_session or not cal.is_session(day):
                    continue
                if cal.session_close(day) + GRACE <= now_ts:
                    return d
        except Exception:  # noqa: BLE001 - an unknown calendar falls back to weekdays
            logger.debug("bar_catch_up: calendar %s unusable for %s", code, symbol, exc_info=True)
    day = today_date - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _newest_bars(db: Session, now: datetime) -> dict[str, date]:
    """Newest bar date of every series maintained in the last :data:`MAINTAINED_DAYS`, plus every paper holding."""
    since = now - timedelta(days=MAINTAINED_DAYS)
    newest: dict[str, date] = {}
    rows = db.execute(
        text("SELECT symbol, MAX(ts) FROM bar_prices WHERE ts >= :since GROUP BY symbol"),
        {"since": since},
    ).all()
    for symbol, ts in rows:
        day = _as_date(ts)
        if symbol and day is not None:
            newest[str(symbol).upper()] = day

    from app.foundation.models.entities import PaperHolding

    held = {
        str(t).upper()
        for (t,) in db.query(PaperHolding.ticker)
        .filter(PaperHolding.ticker.isnot(None), PaperHolding.ticker != "", PaperHolding.quantity > 0)
        .distinct()
        .all()
    }
    for symbol in sorted(held - set(newest)):
        day = _as_date(
            db.execute(text("SELECT MAX(ts) FROM bar_prices WHERE symbol = :s"), {"s": symbol}).scalar()
        )
        if day is not None:  # no bars at all: left to the full backfill paths
            newest[symbol] = day
    return newest


def _quarantined(db: Session) -> set[str]:
    from app.foundation.models.entities import BarSeriesMeta

    return {
        str(s).upper()
        for (s,) in db.query(BarSeriesMeta.symbol).filter(BarSeriesMeta.quarantine_reason.isnot(None)).all()
    }


def lagging_series(db: Session, now: datetime | None = None) -> dict[str, tuple[date, date]]:
    """``{symbol: (newest stored bar, expected session)}`` for every series behind its exchange."""
    now = now or datetime.now(UTC)
    skip = _quarantined(db)
    out: dict[str, tuple[date, date]] = {}
    for symbol, newest in _newest_bars(db, now).items():
        if symbol in skip:
            continue
        expected = last_closed_session(symbol, now)
        if newest < expected:
            out[symbol] = (newest, expected)
    return out


def catch_up_lagging_bars(
    db: Session,
    *,
    now: datetime | None = None,
    max_seconds: float = 900.0,
    ingest: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch the missing days of every lagging series, within *max_seconds*.

    Returns ``{lagging, tried, caught_up, still_lagging, stopped_early, examples}``;
    ``examples`` names up to ten series still behind.
    """
    started = time.monotonic()
    lagging = lagging_series(db, now)
    if ingest is None:
        from app.foundation.data_backbone.ingest import DataIngester

        ingester = DataIngester(db)

        def _ingest(symbol: str, start: str) -> dict[str, Any]:
            return ingester.ingest_bar_prices(symbol, start_date=start)

        ingest = _ingest

    report: dict[str, Any] = {
        "lagging": len(lagging), "tried": 0, "caught_up": 0, "still_lagging": 0,
        "stopped_early": False, "examples": [],
    }
    for symbol, (newest, expected) in sorted(lagging.items()):
        if time.monotonic() - started > max_seconds:
            report["stopped_early"] = True
            break
        report["tried"] += 1
        start = (newest - timedelta(days=1)).isoformat()
        try:
            ingest(symbol, start)
        except Exception as exc:  # noqa: BLE001 - one series never stops the rest
            logger.warning("bar_catch_up: %s from %s failed: %s", symbol, start, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        after = _as_date(db.execute(text("SELECT MAX(ts) FROM bar_prices WHERE symbol = :s"), {"s": symbol}).scalar())
        if after is not None and after >= expected:
            report["caught_up"] += 1
        else:
            report["still_lagging"] += 1
            if len(report["examples"]) < 10:
                report["examples"].append(f"{symbol} {after or newest} < {expected}")
    logger.info("bar_catch_up: %s", report)
    return report


#: A ``split_unadjusted`` series is re-pulled again once its last re-pull is this old.
QUARANTINE_RETRY_AFTER = timedelta(hours=20)


def retry_split_quarantines(
    db: Session,
    *,
    now: datetime | None = None,
    repull: Callable[[str, str | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Re-pull each ``split_unadjusted`` series once a day; a clean re-pull lifts the quarantine.

    The weekly re-pull reaches a series only six days after its last full
    pull, so a split signature from one bad provider answer (SN and INFO on
    2026-11-19, clean again the same morning) kept the name out of the
    universe for about ten days. Returns ``{retried, cleared, still}``.
    """
    from app.foundation.models.entities import BarSeriesMeta

    now = now or datetime.now(UTC)
    cutoff = now - QUARANTINE_RETRY_AFTER
    if repull is None:
        from app.foundation.data_backbone.ingest import DataIngester

        ingester = DataIngester(db)

        def _repull(symbol: str, provider: str | None) -> dict[str, Any]:
            return ingester.repull_series(symbol, provider=provider)

        repull = _repull
    report: dict[str, Any] = {"retried": 0, "cleared": [], "still": []}
    pins = db.query(BarSeriesMeta).filter(BarSeriesMeta.quarantine_reason == "split_unadjusted").all()
    for pin in pins:
        updated = pin.updated_at
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        if updated is not None and updated > cutoff:
            continue
        symbol, provider = pin.symbol, pin.provider
        report["retried"] += 1
        try:
            repull(symbol, provider)
        except Exception as exc:  # noqa: BLE001 - one series never stops the rest
            logger.warning("bar_catch_up: re-pull of quarantined %s failed: %s", symbol, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        db.expire_all()
        after = db.get(BarSeriesMeta, symbol)
        if after is not None and after.quarantine_reason is None:
            report["cleared"].append(symbol)
        else:
            report["still"].append(symbol)
    if report["retried"]:
        logger.info("bar_catch_up: quarantine retry %s", report)
    return report
