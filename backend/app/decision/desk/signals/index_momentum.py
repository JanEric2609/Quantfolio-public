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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from app.foundation.models.entities import DeskEvent
from app.foundation.scalable import market
from app.foundation.scalable.cli import ScalableCli, ScalableError

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


def compute_r1(db: Session, cli: ScalableCli, plan: Plan, now: datetime | None = None) -> Decimal | None:
    """r1 for *plan*. The US bars are read for the week up to *now* (the tick's time), not the wall clock."""
    u = plan.underlying
    end = now or datetime.now(UTC)
    if u.alpaca_symbol:
        provider = news.alpaca_for(db)
        if provider is None:
            return None
        try:
            bars = provider.get_intraday_bars(u.alpaca_symbol, "30Min", end - timedelta(days=7), end)
        except (ValueError, httpx.HTTPError) as exc:  # HTTP 429/5xx (ValueError) or a timeout
            raise DataUnavailable(type(exc).__name__) from exc
        return compute_r1_us(bars, plan.day)
    return compute_r1_xetra(market.get_chart(cli, u.proxy_isin or u.isin, "7d"), plan.day)


def _already_fired(db: Session, signal_key: str) -> bool:
    for ev in db.query(DeskEvent).filter(DeskEvent.kind == "signal").order_by(DeskEvent.seq.desc()).limit(200):
        if json.loads(ev.payload).get("key") == signal_key:
            return True
    return False


@dataclass
class _ProductRead:
    product_type: str
    prep: service.PreparedProposal | None = None
    quote: market.Quote | None = None  # the fill quote, read before any write
    error: str | None = None  # a failed Scalable read: logged, the other products still go ahead


@dataclass
class _PlanRead:
    plan: Plan
    key: str
    reason: str | None = None  # skipped or not fired: skip reason, no_data, r1_zero
    error: str | None = None
    r1: Decimal | None = None
    direction: str = "long"
    products: list[_ProductRead] = field(default_factory=list)
    entry_ref: Decimal | None = None  # the underlying's price for the fills (the placebo's move)
    cfd_ref: Decimal | None = None
    cfd_sigma: Decimal | None = None


def fire_due_signals(db: Session, user_id: str, cli: ScalableCli, now: datetime,
                     opened: list[str] | None = None, errors: list[str] | None = None,
                     clock: Callable[[], datetime] | None = None) -> list[str]:
    """Open this minute's paper positions (one per product type, plus the CFD) for every index whose entry
    time has come.

    Every index's reads come first (r1, both products and their fill quotes, the reference price, the
    CFD's), then the writes, each index in its own savepoint: the first write takes the desk's write lock
    until the stage commits, and no network read happens under it. A failed r1 read (Alpaca, or any sc
    error such as the 6-hourly sync holding the session lock) is retried next minute inside the fire
    window, and on its last minute logged as a ``no_data`` day. A failed product read (the knock-out or
    warrant search) is logged for that product only: the CFD, priced from the reference alone, still
    goes out. An outdated wrapper propagates. *opened* collects the ids of the positions opened, so the
    caller announces exactly this tick's fills; *errors* collects the failed reads' codes (health).
    *clock* (the live monitor: the wall clock) re-reads the time for each quote age and fill; without it
    *now* is used throughout (tests)."""
    fired: list[str] = []
    reads: list[_PlanRead] = []
    day = now.astimezone(UTC).date()
    for plan in plans_for_day(day) + plans_for_day(day - timedelta(days=1)):
        if not (plan.enter_at <= now < plan.enter_at + FIRE_WINDOW):
            continue
        key = f"{plan.key}:{plan.day.isoformat()}"
        if _already_fired(db, key):
            continue
        try:
            read = _read_plan(db, user_id, cli, plan, key, now, clock, errors)
        except market.WrapperOutdated:
            raise
        except (DataUnavailable, ScalableError) as exc:
            code = str(getattr(exc, "code", None) or type(exc).__name__)[:60]
            if errors is not None:
                errors.append(code)
            if now + timedelta(minutes=1) < plan.enter_at + FIRE_WINDOW:
                logger.info("desk index rule %s: read failed (%s), retrying next minute", key, code)
                continue
            read = _PlanRead(plan, key, reason="no_data", error=code)  # the window's last minute
        reads.append(read)
    for read in reads:
        ids: list[str] = []
        with db.begin_nested():
            did_fire = _record_plan(db, user_id, read, now, ids, clock)
        if did_fire:
            fired.append(read.key)
            if opened is not None:
                opened.extend(ids)
    return fired


def _read_plan(db: Session, user_id: str, cli: ScalableCli, plan: Plan, key: str, now: datetime,
               clock: Callable[[], datetime] | None, errors: list[str] | None) -> _PlanRead:
    """All of one index's network reads; writes nothing. Raises only when r1 cannot be read."""
    read = _PlanRead(plan, key)
    skip = skip_reason(plan)
    r1 = None if skip else compute_r1(db, cli, plan, now)
    if skip or r1 is None or r1 == 0:
        read.reason = skip or ("no_data" if r1 is None else "r1_zero")
        return read
    read.r1, read.direction = r1, ("long" if r1 > 0 else "short")
    for product_type in PRODUCT_TYPES:
        product = _ProductRead(product_type)
        try:
            product.prep = service.prepare_proposal(
                db, user_id, signal="index_momentum", underlying=plan.underlying, direction=read.direction,
                product_type=product_type, cli=cli, now=clock() if clock else now, max_exit_at=plan.exit_at,
                clock=clock)
            chosen = product.prep.selection.chosen if product.prep.selection else None
            if chosen is not None and product.prep.refusal is None and product.prep.verdict \
                    and product.prep.verdict.paper_ok:
                product.quote = market.get_quote(cli, chosen.isin)
        except market.WrapperOutdated:
            raise
        except ScalableError as exc:
            product.prep, product.error = None, str(exc.code)[:60]
            if errors is not None:
                errors.append(product.error)
        read.products.append(product)
    at = clock() if clock else now
    read.entry_ref = service.underlying_price(db, cli, plan.underlying, now=at)
    read.cfd_ref, read.cfd_sigma = cfd.prepare_signal(db, cli, plan.underlying, at)
    return read


def _record_plan(db: Session, user_id: str, read: _PlanRead, now: datetime, ids: list[str],
                 clock: Callable[[], datetime] | None) -> bool:
    """One index's writes (no network read): the signal, the products' proposals and fills, the CFD."""
    if read.reason is not None:
        payload = {"key": read.key, "fired": False, "reason": read.reason}
        if read.error:
            payload["error"] = read.error
        append_event(db, "signal", service.MODE, payload, now=now)
        return False
    plan = read.plan
    append_event(db, "signal", service.MODE,
                 {"key": read.key, "fired": True, "r1": str(read.r1), "direction": read.direction}, now=now)
    for product in read.products:
        if product.prep is None:
            append_event(db, "guard_verdict", service.MODE, {
                "signal": "index_momentum", "underlying": plan.underlying.isin, "direction": read.direction,
                "product_type": product.product_type, "ok": False, "reasons": ["read_failed"],
                "error": product.error}, now=now)
            continue
        result = service.record_proposal(db, product.prep)
        if result.position is not None and result.position.status == "proposed":
            if service.fill_with_quote(db, result.position, product.quote, clock() if clock else now,
                                       plan.underlying, read.entry_ref):
                ids.append(result.position.id)
    # The same signal as a manual Trading 212 CFD (cell index_momentum:cfd, paper-scored).
    position = cfd.record_signal(db, user_id, plan.key, plan.underlying, read.direction, plan.exit_at,
                                 clock() if clock else now, read.cfd_ref, read.cfd_sigma)
    if position is not None:
        ids.append(position.id)
    return True
