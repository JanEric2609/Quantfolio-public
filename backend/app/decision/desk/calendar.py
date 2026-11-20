"""Trading days, cut-offs and the macro-event calendar (ADR 0027).

Sessions come from ``exchange_calendars`` (``XETR``, ``XNYS``), each in its own
time zone, so a DST week where Berlin and New York differ by one hour less
needs no special case.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from exchange_calendars.errors import DateOutOfBounds

from .limits import LIMITS

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")

#: Macro events that block an index entry whose holding window contains the date.
#: Maintained yearly like BASISZINS_BY_YEAR; the Control Center warns when next year is
#: missing after 1 December. A date not confirmed on the official page is left out.
MACRO_EVENTS_BY_YEAR: dict[int, dict[str, tuple[str, ...]]] = {
    2026: {
        # FOMC decision days (second day), https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
        "fomc": ("2026-12-09",),
        # ECB monetary policy meetings (press conference day), https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
        "ecb": ("2026-12-17",),
        # US CPI and Employment Situation, https://www.bls.gov/schedule/news_release/cpi.htm and .../empsit.htm
        "cpi": ("2026-12-10",),
        "payrolls": ("2026-12-04",),
    },
    2027: {
        "fomc": ("2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27"),
        "ecb": ("2027-02-04", "2027-03-18", "2027-04-29", "2027-06-10", "2027-07-22", "2027-09-09",
                "2027-10-28", "2027-12-16"),
        "cpi": ("2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12", "2027-06-10",
                "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10"),
        "payrolls": ("2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07", "2027-06-04",
                     "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03"),
    },
}

#: Past 2026 dates are no longer listed on the official pages, so they are not repeated here.


@lru_cache(maxsize=4)
def _cal(code: str):
    return xcals.get_calendar(code)


def is_trading_day(d: date, exchange: str = "XETR") -> bool:
    cal = _cal(exchange)
    try:
        return bool(cal.is_session(d.isoformat()))
    except DateOutOfBounds:
        return d.weekday() < 5


def next_trading_day(d: date, exchange: str = "XETR") -> date:
    nxt = d + timedelta(days=1)
    for _ in range(14):
        if is_trading_day(nxt, exchange):
            return nxt
        nxt += timedelta(days=1)
    return nxt


def is_early_close(d: date, exchange: str) -> bool:
    return pd_ts(d) in set(_cal(exchange).early_closes)


def pd_ts(d: date):
    import pandas as pd

    return pd.Timestamp(d.isoformat())


def session_times(d: date, exchange: str) -> tuple[datetime, datetime] | None:
    """(open, close) of the session on *d* as aware datetimes in the exchange's own zone."""
    if not is_trading_day(d, exchange):
        return None
    cal = _cal(exchange)
    sess: Any = pd_ts(d)
    return (cal.session_open(sess).tz_convert(cal.tz).to_pydatetime(),
            cal.session_close(sess).tz_convert(cal.tz).to_pydatetime())


def cutoff_at(d: date) -> datetime:
    return datetime.combine(d, time(LIMITS.cutoff_hour, LIMITS.cutoff_minute), tzinfo=BERLIN)


def flat_by_day(d: date) -> bool:
    """True when the next calendar day is not a Xetra trading day: everything is closed that evening."""
    return is_trading_day(d, "XETR") and not is_trading_day(d + timedelta(days=1), "XETR")


def cutoff_kind(d: date) -> str:
    return "friday_cutoff" if d.weekday() == 4 else "holiday_cutoff"


def max_exit_at(entry_at: datetime, *, days: int | None = None) -> datetime:
    """The latest exit: 21:30 Berlin on the *days*-th trading day, or the first flat-by day if sooner."""
    days = days or LIMITS.max_holding_trading_days
    day = entry_at.astimezone(BERLIN).date()
    counted = 1
    while True:
        if flat_by_day(day) or counted >= days:
            return cutoff_at(day)
        day = next_trading_day(day)
        counted += 1


def window_start_at(d: date) -> datetime:
    """The start of the desk's trading window on day *d* (Berlin)."""
    return datetime.combine(d, time(LIMITS.window_start_hour, LIMITS.window_start_minute), tzinfo=BERLIN)


def in_trading_window(now: datetime) -> bool:
    local = now.astimezone(BERLIN)
    if not is_trading_day(local.date(), "XETR"):
        return False
    start = time(LIMITS.window_start_hour, LIMITS.window_start_minute)
    end = time(LIMITS.window_end_hour, LIMITS.window_end_minute)
    return start <= local.time() <= end


def underlying_session_open(exchange: str, now: datetime) -> bool:
    """Is the underlying's own exchange open at *now*? (Xetra: 09:00-17:30; NYSE: 09:30-16:00 New York.)"""
    local_day = now.astimezone(_cal(exchange).tz).date()
    times = session_times(local_day, exchange)
    return bool(times and times[0] <= now.astimezone(UTC) <= times[1])


def macro_events_between(start: date, end: date) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for year in {start.year, end.year}:
        for kind, days in MACRO_EVENTS_BY_YEAR.get(year, {}).items():
            out += [(kind, d) for d in days if start.isoformat() <= d <= end.isoformat()]
    return sorted(out, key=lambda x: x[1])


def macro_calendar_covers(year: int) -> bool:
    return year in MACRO_EVENTS_BY_YEAR
