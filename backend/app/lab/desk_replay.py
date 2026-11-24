"""Historical replay of the desk's frozen index rule on US 30-minute bars (ADR 0027 amendment 2026-11-20).

Research only: the replay never changes the pre-registered rule or its pass
test, it answers early whether the rule's edge can pay a product's costs.

The rule (Gao, Han, Li, Zhou 2018): the sign of ``r1``, the return from the
previous session's close to the end of the first half hour, is the direction
of the last half hour. Here ``r_last`` is the 15:30-16:00 New York bar (close
of the 15:30 bar over close of the 15:00 bar); the live desk exits three
minutes earlier. Half days are skipped; ``desk_days`` also skips Fridays and
pre-holidays, as the live US rule does (Scalable's 21:30 cut-off). Macro-event
days are not skipped (the desk's calendar does not reach back to 2016).

Costs, per trade, in EUR:

* **knock-out** at leverage ``ko_leverage``: the desk's sizing (5 EUR loss
  budget less two 0.99 EUR fees over a stop of leverage x daily sigma, clamped
  3-30 %, capped by the 250 EUR pot), the two fees and the quoted spread;
* **CFD** (Trading 212): exposure 5 EUR / daily sigma capped at 1,000 EUR,
  the assumed spread in basis points and the 0.5 % currency fee on the result.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN_BAR, PRE_LAST_BAR, LAST_BAR = time(9, 30), time(15, 0), time(15, 30)


@dataclass(frozen=True)
class Day:
    day: date
    r1: float
    r_last: float
    sigma: float | None  # std of the previous 20 daily close-to-close returns
    friday_or_preholiday: bool


@dataclass(frozen=True)
class Costs:
    ko_leverage: float = 5.0
    ko_spread: float = 0.005  # fraction of the price, round trip
    fee_per_order: float = 0.99
    max_loss: float = 5.0
    pot: float = 250.0
    cfd_spread_bp: float = 2.0
    cfd_fx_fee: float = 0.005
    cfd_max_exposure: float = 1000.0


@lru_cache(maxsize=1)
def _xetra():
    import exchange_calendars as xcals

    return xcals.get_calendar("XETR")


def _xetra_session(d: date) -> bool:
    from exchange_calendars.errors import DateOutOfBounds

    try:
        return bool(_xetra().is_session(d.isoformat()))
    except DateOutOfBounds:
        return d.weekday() < 5


def xetra_flat_by(d: date) -> bool:
    """The live rule's skip for the US half (``signals.index_momentum.skip_reason``): the desk is flat by
    21:30 Berlin, before the 21:57 exit, on a Xetra trading day whose next calendar day is not one
    (``calendar.flat_by_day``; lab may not import the desk, a test pins the two equal). The live rule's
    macro-day skip is not replayed: the desk's macro calendar starts in 2026."""
    return _xetra_session(d) and not _xetra_session(d + timedelta(days=1))


def days_from_bars(bars: list[dict[str, Any]]) -> list[Day]:
    """Bars ``{"t": aware datetime (bar start), "c": close}`` of one symbol -> one row per full session."""
    by_day: dict[date, dict[time, float]] = defaultdict(dict)
    for b in bars:
        local = b["t"].astimezone(NEW_YORK)
        if OPEN_BAR <= local.time() <= LAST_BAR and b.get("c"):
            by_day[local.date()][local.time()] = float(b["c"])
    sessions = sorted(by_day)
    closes: list[float] = []
    out: list[Day] = []
    for i, d in enumerate(sessions):
        bars_d = by_day[d]
        last_close = bars_d[max(bars_d)]
        full = LAST_BAR in bars_d and PRE_LAST_BAR in bars_d and OPEN_BAR in bars_d
        if full and closes:
            rets = [math.log(closes[j] / closes[j - 1]) for j in range(max(1, len(closes) - 20), len(closes))]
            sigma = statistics.stdev(rets) if len(rets) >= 20 else None
            out.append(Day(
                d, bars_d[OPEN_BAR] / closes[-1] - 1, bars_d[LAST_BAR] / bars_d[PRE_LAST_BAR] - 1, sigma,
                xetra_flat_by(d),
            ))
        closes.append(last_close)
    return out


def _stats(values: list[float]) -> dict[str, float | int | None]:
    n = len(values)
    if n < 2:
        return {"n": n, "mean": values[0] if values else None, "t": None, "hit": None, "total": sum(values)}
    sd = statistics.stdev(values)
    mean = statistics.fmean(values)
    return {"n": n, "mean": mean, "t": mean / (sd / math.sqrt(n)) if sd else None,
            "hit": sum(1 for v in values if v > 0) / n, "total": sum(values)}


def trade_results(day: Day, costs: Costs) -> dict[str, float] | None:
    """Gross return and the EUR result of a knock-out and a CFD on one day; None when no trade."""
    if day.r1 == 0 or day.sigma is None or day.sigma <= 0:
        return None
    gross = math.copysign(1.0, day.r1) * day.r_last
    lev = costs.ko_leverage
    stop_frac = min(max(lev * day.sigma, 0.03), 0.30)
    notional = min((costs.max_loss - 2 * costs.fee_per_order) / stop_frac, costs.pot)
    ko = notional * lev * gross - 2 * costs.fee_per_order - costs.ko_spread * notional
    exposure = min(costs.max_loss / day.sigma, costs.cfd_max_exposure)
    cfd = exposure * gross - exposure * costs.cfd_spread_bp / 10000 - costs.cfd_fx_fee * abs(exposure * gross)
    return {"gross_bp": gross * 10000, "ko_eur": ko, "cfd_eur": cfd, "ko_notional": notional,
            "cfd_exposure": exposure}


def evaluate(days: list[Day], costs: Costs, *, desk_days: bool = True) -> dict[str, Any]:
    rows = [(d, r) for d in days if not (desk_days and d.friday_or_preholiday)
            if (r := trade_results(d, costs)) is not None]
    by_year: dict[int, list[float]] = defaultdict(list)
    for d, r in rows:
        by_year[d.day.year].append(r["gross_bp"])
    return {
        "first": rows[0][0].day.isoformat() if rows else None, "last": rows[-1][0].day.isoformat() if rows else None,
        "gross_bp": _stats([r["gross_bp"] for _, r in rows]),
        "knockout_eur": _stats([r["ko_eur"] for _, r in rows]),
        "cfd_eur": _stats([r["cfd_eur"] for _, r in rows]),
        "median_ko_notional": statistics.median(r["ko_notional"] for _, r in rows) if rows else None,
        "median_cfd_exposure": statistics.median(r["cfd_exposure"] for _, r in rows) if rows else None,
        "by_year_gross_bp": {y: _stats(v) for y, v in sorted(by_year.items())},
    }


# --- Exit variants on 1-minute bars (pre-registered 2026-11-24, before the data was read) -------------------
#
# The owner's question after three real trades (DAX, ES50, SHOE): they were green at first and red at the fixed
# exit; would a take-profit help? On 30-minute bars the whole trade is one bar, so the path is invisible. These
# variants replay the same entries (15:30 New York, the sign of r1) on 1-minute bars to the live exit (15:57):
#
# * ``fixed``: the rule as registered, the baseline;
# * ``tp_0.5`` / ``tp_1.0``: take profit at 0.5 / 1.0 x the typical 27-minute move (daily sigma x sqrt(27/390),
#   the "typical last-30-minute move" on the CFD message); a 1-minute high/low through the level fills at it;
# * ``trail``: once 0.5 x that move in profit, exit at the first 1-minute close that gives back half the best
#   favourable move.
#
# Every variant keeps the one-daily-sigma stop (filled at the stop level). Three variants against one
# baseline: Holm over the three paired tests. The variants are fixed here; changing a multiple after
# seeing the result makes it a new trial.

ENTRY_MINUTE, EXIT_MINUTE, R1_MINUTE = time(15, 29), time(15, 56), time(9, 59)  # bar starts; closes at +1 min
SESSION_LAST_MINUTE = time(15, 59)
TYPICAL_MINUTES = 27
EXIT_VARIANTS: dict[str, tuple[str, float]] = {"fixed": ("fixed", 0.0), "tp_0.5": ("tp", 0.5),
                                               "tp_1.0": ("tp", 1.0), "trail": ("trail", 0.5)}


@dataclass(frozen=True)
class MinuteDay:
    day: date
    r1: float
    entry: float
    path: tuple[tuple[float, float, float], ...]  # (high, low, close) of each bar 15:30 .. 15:56
    sigma: float | None
    friday_or_preholiday: bool


def minute_days(rows: list[tuple[datetime, float, float, float]]) -> list[MinuteDay]:
    """Rows ``(bar start, high, low, close)`` of one symbol, at least 09:59 and 15:29-15:59 New York. Bars outside
    the regular session (Alpaca's 1-minute bars include extended hours) are dropped, so the previous close is
    the 15:59 bar's and never an after-hours print."""
    by_day: dict[date, dict[time, tuple[float, float, float]]] = defaultdict(dict)
    for t, h, lo, c in rows:
        local = t.astimezone(NEW_YORK)
        if OPEN_BAR <= local.time() <= SESSION_LAST_MINUTE:
            by_day[local.date()][local.time()] = (h, lo, c)
    closes: list[float] = []
    out: list[MinuteDay] = []
    for d in sorted(by_day):
        bars = by_day[d]
        last = bars[max(bars)][2]
        need = [R1_MINUTE, ENTRY_MINUTE] + [time(15, m) for m in range(30, EXIT_MINUTE.minute + 1)]
        if all(k in bars for k in need) and closes:
            rets = [math.log(closes[j] / closes[j - 1]) for j in range(max(1, len(closes) - 20), len(closes))]
            sigma = statistics.stdev(rets) if len(rets) >= 20 else None
            path = tuple(bars[time(15, m)] for m in range(30, EXIT_MINUTE.minute + 1))
            out.append(MinuteDay(d, bars[R1_MINUTE][2] / closes[-1] - 1, bars[ENTRY_MINUTE][2], path, sigma,
                                 xetra_flat_by(d)))
        if max(bars) == SESSION_LAST_MINUTE:
            closes.append(last)
    return out


def exit_return(day: MinuteDay, variant: str) -> float | None:
    """The trade's gross return (signed by the direction) under *variant*; None when no trade."""
    if day.r1 == 0 or day.sigma is None or day.sigma <= 0:
        return None
    kind, mult = EXIT_VARIANTS[variant]
    side = math.copysign(1.0, day.r1)
    unit = day.sigma * math.sqrt(TYPICAL_MINUTES / 390)
    stop, best = -day.sigma, 0.0
    for h, lo, c in day.path:
        fav = (h if side > 0 else lo) / day.entry - 1
        adv = (lo if side > 0 else h) / day.entry - 1
        if side * adv <= stop:
            return stop
        if kind == "tp" and side * fav >= mult * unit:
            return mult * unit
        now = side * (c / day.entry - 1)
        best = max(best, now)
        if kind == "trail" and best >= mult * unit and now <= best / 2:
            return now
    return side * (day.path[-1][2] / day.entry - 1)


def evaluate_exits(days: list[MinuteDay], costs: Costs, *, desk_days: bool = True) -> dict[str, Any]:
    """Per variant: CFD EUR per trade (the desk's exposure and costs) and the paired test against ``fixed``."""
    from scipy import stats as st

    rows = [d for d in days if not (desk_days and d.friday_or_preholiday) and exit_return(d, "fixed") is not None]
    eur: dict[str, list[float]] = {}
    for v in EXIT_VARIANTS:
        vals = []
        for d in rows:
            g = exit_return(d, v) or 0.0
            exposure = min(costs.max_loss / (d.sigma or 1), costs.cfd_max_exposure)
            vals.append(exposure * g - exposure * costs.cfd_spread_bp / 10000 - costs.cfd_fx_fee * abs(exposure * g))
        eur[v] = vals
    out: dict[str, Any] = {"first": rows[0].day.isoformat() if rows else None,
                           "last": rows[-1].day.isoformat() if rows else None, "variants": {}}
    p_raw: dict[str, float] = {}
    for v, vals in eur.items():
        entry: dict[str, Any] = {"cfd_eur": _stats(vals)}
        if v != "fixed" and len(vals) > 2:
            diff = [a - b for a, b in zip(vals, eur["fixed"], strict=True)]
            d_stats = _stats(diff)
            t = d_stats["t"]
            p = float(2 * st.t.sf(abs(t), len(diff) - 1)) if t is not None else 1.0
            entry["vs_fixed"] = {**d_stats, "p_two_sided": p}
            p_raw[v] = p
        out["variants"][v] = entry
    ordered = sorted(p_raw, key=p_raw.__getitem__)
    running = 0.0
    for i, v in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - i) * p_raw[v]))
        out["variants"][v]["vs_fixed"]["p_holm"] = running
    return out
