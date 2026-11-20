"""The frozen index intraday-momentum rule (Gao, Han, Li, Zhou 2018), no tuned parameter (ADR 0027).

``r1`` is the return from the previous session's close to 30 minutes after the
open. Its sign is the direction. Enter 30 minutes before the close, exit three
minutes before it. Skip a day with ``r1 == 0``, a half day, a macro event on the
day, or (US) a Friday or pre-holiday, when the 21:30 Berlin cut-off falls
before the planned exit.

Each fired signal opens a paper knock-out, a paper warrant and a paper CFD
(``cfd.py``; the owner trades that one by hand on Trading 212).

Which indices fire: DAX and Euro Stoxx 50 on Xetra hours (ETF proxies from sc's
30-minute chart), S&P 500 and Nasdaq-100 on NYSE hours (SPY and QQQ 30-minute
bars from Alpaca's free IEX feed).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskEvent
from app.foundation.scalable import market
from app.foundation.scalable.cli import ScalableCli

from .. import calendar as cal
from .. import cfd, news, service
from ..ledger import append_event
from ..underlyings import ALIASES, Underlying

logger = logging.getLogger(__name__)

INDEX_KEYS = ("DAX", "ES50", "SPX", "NDX")
PRODUCT_TYPES = ("knockout", "warrant")
ENTRY_BEFORE_CLOSE = timedelta(minutes=30)
EXIT_BEFORE_CLOSE = timedelta(minutes=3)  # issuers stop quoting at 22:00 Berlin: two monitor retries before it
FIRE_WINDOW = timedelta(minutes=5)  # the monitor ticks every minute; a missed tick may still fire within this


@dataclass(frozen=True)
class Plan:
    key: str
    underlying: Underlying
    day: date
    enter_at: datetime
    exit_at: datetime


def plans_for_day(day: date) -> list[Plan]:
    out: list[Plan] = []
    for key in INDEX_KEYS:
        u = ALIASES[key]
        times = cal.session_times(day, u.exchange)
        if times is None:
            continue
        close = times[1]
        out.append(Plan(key, u, day, (close - ENTRY_BEFORE_CLOSE).astimezone(UTC), (close - EXIT_BEFORE_CLOSE).astimezone(UTC)))
    return out


def skip_reason(plan: Plan) -> str | None:
    u = plan.underlying
    if cal.is_early_close(plan.day, u.exchange):
        return "half_day"
    if cal.macro_events_between(plan.day, plan.day):
        return "macro_event"
    berlin_day = plan.exit_at.astimezone(cal.BERLIN).date()
    if cal.flat_by_day(berlin_day) and plan.exit_at.astimezone(cal.BERLIN) > cal.cutoff_at(berlin_day):
        return "cutoff_before_exit"
    return None


def _nearest(points: list[tuple[datetime, Decimal]], target: datetime, tolerance: timedelta) -> Decimal | None:
    best = min(points, key=lambda p: abs(p[0] - target), default=None)
    return best[1] if best and abs(best[0] - target) <= tolerance else None


def compute_r1_xetra(points: list[tuple[datetime, Decimal]], day: date) -> Decimal | None:
    """Previous close (last point at or before 17:30 Berlin of the previous session) to the 09:30 point."""
    prev = [p for p in points if p[0].astimezone(cal.BERLIN).date() < day]
    if not prev:
        return None
    prev_day = max(p[0].astimezone(cal.BERLIN).date() for p in prev)
    close_cut = datetime.combine(prev_day, time(17, 30), tzinfo=cal.BERLIN)
    closes = [p for p in prev if p[0].astimezone(cal.BERLIN).date() == prev_day and p[0] <= close_cut]
    if not closes:
        return None
    prev_close = max(closes, key=lambda p: p[0])[1]
    target = datetime.combine(day, time(9, 30), tzinfo=cal.BERLIN)
    px = _nearest([p for p in points if p[0].astimezone(cal.BERLIN).date() == day], target, timedelta(minutes=15))
    return (px / prev_close - 1) if px is not None and prev_close else None


#: Regular-session 30-minute bar starts (New York). Alpaca's bars include pre- and after-market
#: trading; the registered rule and the replay (``lab/desk_replay.py``) use the regular session only.
US_FIRST_BAR, US_LAST_BAR = time(9, 30), time(15, 30)


class DataUnavailable(RuntimeError):
    """The price read failed (network, rate limit): try again next minute, do not log a no-data day."""


def compute_r1_us(bars: list[dict], day: date) -> Decimal | None:
    """Previous session's regular close (the 15:30 bar's close, i.e. 16:00) to the close of the first
    30-minute bar of *day* (New York). Extended-hours bars are ignored, as in the replay."""
    def local(b):
        return b["t"].astimezone(cal.NEW_YORK)
    prev = [b for b in bars if local(b).date() < day and b["c"] is not None
            and US_FIRST_BAR <= local(b).time() <= US_LAST_BAR]
    first = [b for b in bars if local(b).date() == day and local(b).time() == time(9, 30) and b["c"] is not None]
    if not prev or not first:
        return None
    prev_close = max(prev, key=lambda b: b["t"])["c"]
    return (first[0]["c"] / prev_close - 1) if prev_close else None


def compute_r1(db: Session, cli: ScalableCli, plan: Plan) -> Decimal | None:
    u = plan.underlying
    if u.alpaca_symbol:
        provider = news.alpaca_for(db)
        if provider is None:
            return None
        try:
            bars = provider.get_intraday_bars(u.alpaca_symbol, "30Min", datetime.now(UTC) - timedelta(days=7),
                                              datetime.now(UTC))
        except (ValueError, httpx.HTTPError) as exc:  # HTTP 429/5xx (ValueError) or a timeout
            raise DataUnavailable(type(exc).__name__) from exc
        return compute_r1_us(bars, plan.day)
    return compute_r1_xetra(market.get_chart(cli, u.proxy_isin or u.isin, "7d"), plan.day)


def _already_fired(db: Session, signal_key: str) -> bool:
    for ev in db.query(DeskEvent).filter(DeskEvent.kind == "signal").order_by(DeskEvent.seq.desc()).limit(200):
        if json.loads(ev.payload).get("key") == signal_key:
            return True
    return False


def fire_due_signals(db: Session, user_id: str, cli: ScalableCli, now: datetime) -> list[str]:
    """Open this minute's paper positions (one per product type) for every index whose entry time has come."""
    fired: list[str] = []
    day = now.astimezone(UTC).date()
    for plan in plans_for_day(day) + plans_for_day(day - timedelta(days=1)):
        if not (plan.enter_at <= now < plan.enter_at + FIRE_WINDOW):
            continue
        key = f"{plan.key}:{plan.day.isoformat()}"
        if _already_fired(db, key):
            continue
        skip = skip_reason(plan)
        try:
            r1 = None if skip else compute_r1(db, cli, plan)
        except DataUnavailable as exc:
            if now + timedelta(minutes=1) < plan.enter_at + FIRE_WINDOW:
                logger.info("desk index rule %s: price read failed (%s), retrying next minute", key, exc)
                continue
            r1 = None  # the window's last minute: logged as a no-data day
        if skip or r1 is None or r1 == 0:
            reason = skip or ("no_data" if r1 is None else "r1_zero")
            append_event(db, "signal", service.MODE, {"key": key, "fired": False, "reason": reason}, now=now)
            continue
        direction = "long" if r1 > 0 else "short"
        append_event(db, "signal", service.MODE,
                     {"key": key, "fired": True, "r1": str(r1), "direction": direction}, now=now)
        for product_type in PRODUCT_TYPES:
            result = service.propose(
                db, user_id, signal="index_momentum", underlying=plan.underlying, direction=direction,
                product_type=product_type, cli=cli, now=now, max_exit_at=plan.exit_at)
            if result.position is not None and result.position.status == "proposed":
                service.open_directly(db, result.position, cli, now, plan.underlying)
        # The same signal as a manual Trading 212 CFD (cell index_momentum:cfd, paper-scored).
        cfd.open_signal(db, user_id, plan.key, plan.underlying, direction, plan.exit_at, cli, now)
        fired.append(key)
    return fired
