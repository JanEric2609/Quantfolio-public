"""Candidate sources of the event desk: hype and news drift (ADR 0028). Reads only; nothing here writes.

``news_wire_items`` is read here to open *paper* positions in their registered cells, and nowhere else to act
(ADR 0028 changed ADR 0027's invariant). Each ``prepare_*`` may use the network; the caller writes afterwards.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.foundation import technical
from app.foundation.models.entities import NewsWireItem
from app.foundation.providers.alpaca_provider import AlpacaProvider, DataUnavailable

from . import calendar as cal
from . import cfd_instruments as ci
from .underlyings import daily_sigma

logger = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
RULES_HYPE = technical.EVENT_RULES["hype_fade"]
RULES_NEWS = technical.EVENT_RULES["news_drift"]
RULES_STRONG = technical.EVENT_RULES["strong_event"]
EARNINGS_PATTERNS = re.compile(r"earnings|results|\bQ[1-4]\b|guidance|revenue", re.I)
PRIMARY_SOURCES = ("sec_8k", "de_adhoc")
MAX_SEEDS = 20
#: The screener pre-filter lets through names at 3/4 of the rule's day change (6 % for the 8 % rule).
SCREENER_SLACK = 0.75


@dataclass(frozen=True)
class Candidate:
    cell: str
    inst: ci.CfdInstrument
    direction: str
    ref_price: Decimal
    stop_distance: Decimal
    max_exit_at: datetime
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    #: What the owner sees in Trading 212 (index points, or the share price): ``stop_distance`` is in these
    #: units. ``None`` = the reference price itself (a stock).
    level: Decimal | None = None


def in_hype_window(now: datetime) -> bool:
    local = now.astimezone(NY)
    if local.weekday() > 4 or not cal.is_trading_day(local.date(), "XNYS"):
        return False
    minutes = local.hour * 60 + local.minute
    return 10 * 60 + 30 <= minutes <= 15 * 60 + 30


def exit_after_days(now: datetime, days: int, exchange: str) -> datetime:
    """The evaluation time of the exchange *days* trading days on (US 21:40 Berlin, Xetra 17:10)."""
    d = now.astimezone(cal.BERLIN).date()
    for _ in range(days):
        d = cal.next_trading_day(d, exchange)
    hh, mm = (21, 40) if exchange == "XNYS" else (17, 10)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=cal.BERLIN).astimezone(UTC)


def _symbols(item: NewsWireItem) -> list[str]:
    return [s.strip().upper() for s in (item.symbols or "").split(",") if s.strip()]


def _is_earnings(headline: str) -> bool:
    return bool(EARNINGS_PATTERNS.search(headline or ""))


def primary_today(db: Session, symbol: str, since: datetime) -> bool:
    """A filing, ad-hoc disclosure or earnings headline for *symbol* since *since*."""
    rows = db.query(NewsWireItem).filter(NewsWireItem.received_at >= since).all()
    for r in rows:
        if symbol.upper() not in _symbols(r):
            continue
        if r.source in PRIMARY_SOURCES or (r.source == "alpaca" and _is_earnings(r.headline)):
            return True
    return False


def _daily(provider: AlpacaProvider, symbol: str, now: datetime, days: int = 40) -> list[dict[str, Any]]:
    return provider.get_intraday_bars(symbol, "1Day", now - timedelta(days=days), now)


def _atr(rows: list[dict[str, Any]], today) -> float | None:
    bars = [technical.Bar(r["t"].date(), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]))
            for r in rows if r.get("c") is not None and r["t"].date() < today]
    return technical.atr(bars, 14)


def prepare_hype(db: Session, provider: AlpacaProvider | None, now: datetime,
                 exclude: set[str] | None = None, errors: list[str] | None = None) -> list[Candidate]:
    """Short candidates among US stocks that jumped on news and volume with no primary source today."""
    if provider is None or not in_hype_window(now):
        return []
    exclude = exclude or set()
    try:
        movers = provider.screener_movers(50)
        seeds = provider.screener_most_actives(100)
    except DataUnavailable:
        movers, seeds = [], []
        logger.info("desk hype: screener not available, using the news wire")
        if errors is not None:
            errors.append("screener_unavailable")
        movers = _wire_seeds(db, now)
    except (ValueError, httpx.HTTPError, KeyError, TypeError) as exc:
        logger.info("desk hype screener failed: %s", type(exc).__name__)
        if errors is not None:
            errors.append("screener_failed")
        return []
    names = {m["symbol"]: m for m in movers}
    for s in seeds[:MAX_SEEDS]:
        names.setdefault(s["symbol"], {"symbol": s["symbol"], "percent_change": None, "price": None})
    out: list[Candidate] = []
    since = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    today = now.astimezone(NY).date()
    for sym, row in list(names.items())[:60]:
        if sym in exclude or not _may_qualify(row):
            continue
        cand = _hype_one(db, provider, sym, row, now, since, today)
        if cand is not None:
            out.append(cand)
    return out


def _may_qualify(row: dict[str, Any]) -> bool:
    """A cheap pre-filter on the screener's own change (Alpaca gives it in percent: 12.5 = +12.5 %), so daily
    bars are fetched only for names that can pass. Loose on purpose (``SCREENER_SLACK``): the screener can lag
    the bars, and the rule itself is decided on the bars. Rows without a change (most-actives, news seeds)
    always go on to the full check."""
    pc = row.get("percent_change")
    if pc is None:
        return True
    try:
        return float(pc) / 100 >= RULES_HYPE["min_day_change"] * SCREENER_SLACK
    except (TypeError, ValueError):
        return True


def _wire_seeds(db: Session, now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(hours=24)
    rows = db.query(NewsWireItem).filter(NewsWireItem.received_at >= since, NewsWireItem.source == "alpaca",
                                         NewsWireItem.materiality >= 2).all()
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        for s in _symbols(r):
            seen.setdefault(s, {"symbol": s, "percent_change": None, "price": None})
    return list(seen.values())[:MAX_SEEDS]


def _hype_one(db: Session, provider: AlpacaProvider, sym: str, row: dict[str, Any], now: datetime,
              since: datetime, today) -> Candidate | None:
    try:
        rows = _daily(provider, sym, now)
        done = [r for r in rows if r["t"].astimezone(NY).date() < today and r.get("c")]
        cur = next((r for r in rows if r["t"].astimezone(NY).date() == today and r.get("c")), None)
        if cur is None or len(done) < 21:
            return None
        prev_close = float(done[-1]["c"])
        price = float(cur["c"])
        change = price / prev_close - 1
        if change < RULES_HYPE["min_day_change"] or price < RULES_HYPE["min_price_usd"]:
            return None
        base_vol = sum(float(r["v"] or 0) for r in done[-20:]) / 20
        vol_ratio = float(cur["v"] or 0) / base_vol if base_vol > 0 else 0.0
        if vol_ratio < RULES_HYPE["volume_multiple"]:
            return None
        n_today = len(provider.get_news([sym], now - timedelta(hours=24), limit=50))
        n_base = len(provider.get_news([sym], now - timedelta(days=21), limit=50)) - n_today
        per_day = max(n_base, 0) / RULES_HYPE["baseline_days"]
        news_ratio = n_today / per_day if per_day > 0 else float(n_today)
        if n_today < RULES_HYPE["news_min_count"] or news_ratio < RULES_HYPE["news_multiple"]:
            return None
        if primary_today(db, sym, since):
            return None
        headlines = provider.get_news([sym], since, limit=50)
        if any(_is_earnings(h["headline"]) for h in headlines):
            return None
        atr = _atr(rows, today)
        inst = ci.for_symbol(db, sym)
        if atr is None or inst is None:
            return None
    except (ValueError, httpx.HTTPError, KeyError, TypeError, ArithmeticError) as exc:
        logger.info("desk hype read failed for a symbol: %s", type(exc).__name__)
        return None
    dist = Decimal(str(RULES_HYPE["stop_atr"])) * Decimal(str(round(atr, 6)))
    return Candidate(
        cell="hype_fade:cfd", inst=inst, direction="short", ref_price=Decimal(str(price)), stop_distance=dist,
        max_exit_at=exit_after_days(now, RULES_HYPE["max_hold_days"], "XNYS"),
        reason=(f"hype fade. {change:+.1%} today on {news_ratio:.0f}x its usual news and {vol_ratio:.0f}x its "
                "usual volume; no filing or earnings."),
        evidence={"change": round(change, 4), "volume_ratio": round(vol_ratio, 1), "news_ratio": round(news_ratio, 1),
                  "news_today": n_today})


def prepare_news(db: Session, cli: Any, now: datetime, errors: list[str] | None = None) -> list[Candidate]:
    """Primary-source releases of the last 30 minutes that the local model rates 3 and the stock already follows."""
    since = now - timedelta(minutes=RULES_NEWS["min_age_minutes"])
    rows = db.query(NewsWireItem).filter(
        NewsWireItem.received_at >= since, NewsWireItem.materiality >= RULES_NEWS["min_materiality"],
        NewsWireItem.scope == RULES_NEWS["scope"], NewsWireItem.direction.in_(("up", "down"))).all()
    out: list[Candidate] = []
    seen: set[str] = set()
    for r in rows:
        if not (r.source in PRIMARY_SOURCES or (r.source == "alpaca" and _is_earnings(r.headline))):
            continue
        for sym in _symbols(r):
            inst = ci.for_symbol(db, sym)
            if inst is None or inst.kind != "stock" or inst.key in seen:
                continue
            exchange = inst.underlying.exchange
            if not cal.underlying_session_open(exchange, now) or _in_opening_minutes(exchange, now):
                continue
            cand = _news_one(db, cli, inst, r, now)
            if cand is not None:
                seen.add(inst.key)
                out.append(cand)
    return out


def _in_opening_minutes(exchange: str, now: datetime, minutes: int | None = None) -> bool:
    """The first ``open_delay_minutes`` of the session: the opening prints are thin and jumpy (SHOE on
    2026-11-24 printed 10.45 at the open and traded near 11 three minutes later), so no news entry yet."""
    day = now.astimezone(NY if exchange == "XNYS" else cal.BERLIN).date()
    times = cal.session_times(day, exchange)
    if times is None:
        return False
    return now < times[0].astimezone(UTC) + timedelta(minutes=RULES_NEWS["open_delay_minutes"] if minutes is None else minutes)


def strong_signals(db: Session, now: datetime, side: str, today) -> dict[str, Any]:
    """Which of the three extra signals hold for an index move *side* (``up``/``down``): a market headline rated 3
    in that direction, a macro release today, two independent wire sources in that direction (all within the
    news window, rated by the local model)."""
    since = now - timedelta(minutes=RULES_STRONG["news_window_minutes"])
    rows = db.query(NewsWireItem).filter(
        NewsWireItem.received_at >= since, NewsWireItem.scope == RULES_STRONG["scope"],
        NewsWireItem.direction == side, NewsWireItem.materiality >= RULES_STRONG["second_source_min_materiality"]).all()
    headline = next((r for r in rows if (r.materiality or 0) >= RULES_STRONG["min_materiality"]), None)
    sources = sorted({r.source for r in rows})
    macro = [k for k, _d in cal.macro_events_between(today, today)]
    held = {"headline": headline is not None, "macro": bool(macro), "second_source": len(sources) >= 2}
    return {"held": held, "count": sum(held.values()), "macro": macro, "sources": sources,
            "headline": (headline.headline or "")[:120] if headline else ""}


def prepare_strong(db: Session, cli: Any, now: datetime, errors: list[str] | None = None) -> list[Candidate]:
    """Strong events (ADR 0028, amendment 2026-11-26): an index CFD whose day move is at least
    ``min_move_sigmas`` trailing sigma, still holds at least ``hold_fraction`` of it, and at least
    ``min_extra_signals`` of three further signals point the same way. Rare by construction: at most
    ``max_per_week`` in any 7 days (counted by the caller's limits and here on the ledger)."""
    from app.foundation.models.entities import DeskPosition  # local: keeps the import list of this module short

    week_ago = now.astimezone(UTC) - timedelta(days=7)
    if db.query(DeskPosition).filter(DeskPosition.cell == "strong_event:cfd", DeskPosition.created_at >= week_ago).count() \
            >= RULES_STRONG["max_per_week"]:
        return []
    out: list[Candidate] = []
    for inst in ci.INDICES:
        exchange = inst.underlying.exchange
        if not cal.underlying_session_open(exchange, now) or _in_opening_minutes(exchange, now, RULES_STRONG["open_delay_minutes"]):
            continue
        today = now.astimezone(NY if exchange == "XNYS" else cal.BERLIN).date()
        try:
            bars = ci.daily_bars(db, inst, days=90)
            if not bars or bars[-1].date != today:
                continue
            past = [b for b in bars if b.date < today]
            if len(past) < RULES_STRONG["sigma_days"] + 2:
                continue
            sigma = daily_sigma([b.close for b in past], RULES_STRONG["sigma_days"])
            atr = technical.atr(past, technical.EVENT_RULES["atr"])
            if sigma is None or sigma <= 0 or not atr:
                continue
            last, prev = bars[-1], past[-1].close
            move = last.close / prev - 1
            if abs(move) < RULES_STRONG["min_move_sigmas"] * float(sigma):
                continue
            sign = 1 if move > 0 else -1
            extreme = last.high if sign > 0 else last.low
            if sign * (last.close - prev) < RULES_STRONG["hold_fraction"] * sign * (extreme - prev):
                continue  # the move has given back more than a quarter
            sig = strong_signals(db, now, "up" if sign > 0 else "down", today)
            if sig["count"] < RULES_STRONG["min_extra_signals"]:
                continue
            ref = ci.reference_price(db, cli, inst, now, errors)
        except (ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
            logger.info("desk strong event read failed: %s", type(exc).__name__)
            continue
        if ref is None:
            continue
        why = [k.replace("_", " ") for k, v in sig["held"].items() if v]
        out.append(Candidate(
            cell="strong_event:cfd", inst=inst, direction="long" if sign > 0 else "short", ref_price=ref,
            stop_distance=Decimal(str(RULES_STRONG["stop_atr"])) * Decimal(str(round(atr, 6))),
            max_exit_at=exit_after_days(now, RULES_STRONG["backstop_hold_days"], exchange),
            reason=(f"strong event. {inst.key.split(':')[-1]} is {move:+.1%} today ({abs(move) / float(sigma):.1f} sigma) "
                    f"and holding it; also: {', '.join(why)}."),
            evidence={"move": round(move, 4), "sigmas": round(abs(move) / float(sigma), 1), "signals": sig["held"],
                      "macro": sig["macro"], "sources": sig["sources"], "headline": sig["headline"]},
            level=Decimal(str(last.close))))
    return out


def _news_one(db: Session, cli: Any, inst: ci.CfdInstrument, item: NewsWireItem, now: datetime) -> Candidate | None:
    try:
        ref = ci.reference_price(db, cli, inst, now)
        bars = ci.daily_bars(db, inst, days=90)
        today = now.astimezone(NY if inst.underlying.exchange == "XNYS" else cal.BERLIN).date()
        past = [b for b in bars if b.date < today]
        if ref is None or len(past) < RULES_NEWS["sigma_days"] + 2:
            return None
        sigma = daily_sigma([b.close for b in past], RULES_NEWS["sigma_days"])
        if sigma is None or sigma <= 0:
            return None
        move = float(ref) / past[-1].close - 1
        sign = 1 if item.direction == "up" else -1
        if sign * move < RULES_NEWS["move_sigmas"] * float(sigma):
            return None
        atr = technical.atr(past, 14)
    except (ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
        logger.info("desk news read failed: %s", type(exc).__name__)
        return None
    if not atr:
        return None
    return Candidate(
        cell="news_drift:cfd", inst=inst, direction="long" if sign > 0 else "short", ref_price=ref,
        stop_distance=Decimal(str(RULES_NEWS["stop_atr"])) * Decimal(str(round(atr, 6))),
        max_exit_at=exit_after_days(now, RULES_NEWS["max_hold_days"], inst.underlying.exchange),
        reason=(f"news drift. {item.source.replace('_', ' ')} release rated {item.materiality} of 3 "
                f"({item.direction}); the stock is already {move:+.1%} ({abs(move) / float(sigma):.1f} sigma)."),
        evidence={"headline": (item.headline or "")[:120], "source": item.source, "move": round(move, 4),
                  "sigmas": round(abs(move) / float(sigma), 1)})
