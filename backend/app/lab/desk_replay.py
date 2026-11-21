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
from datetime import date, time, timedelta
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
