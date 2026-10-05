"""Time-weighted return of the real book (every broker), in EUR, with an IRR.

The Quant Lab used to plot today's weights applied to two years of prices:
a hindsight index in which a fund bought last month had been held all along
and every savings-plan purchase counted as return. The book's own return is
a time-weighted unit value (GIPS 2020, provision 2.A.20 ff.), here valued
daily:

    r_t = (sum_i q_i(t-1) P_i(t) + D_t) / sum_i q_i(t-1) P_i(t-1) - 1
    U_t = U_(t-1) (1 + r_t),       U_0 = 100

``q_i(t)`` is the quantity of instrument ``i`` held at the end of day ``t``
(``BookPositionSnapshot``, carried forward between snapshots), ``P_i`` its
close in EUR (``eur_prices``, the broker's own price where no market close
exists) and ``D_t`` dividends paid that day. The day's buys and sells are
flows at the close, ``C_t = V_t - sum_i q_i(t-1) P_i(t) - D_t``, so they never
count as return. This is the textbook ``U_(i+1) = U_i V_(i+1) / (V_i + C_i)``
with flows booked at end of day.

The money-weighted return (XIRR) of the same flows says what the investor
earned on the money actually put in, timing included. Neither is annualised
over less than a year (GIPS 2.A.24).
"""
from __future__ import annotations

import logging
import math
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from scipy.optimize import brentq
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# A price older than this many calendar days is not carried forward.
MAX_PRICE_CARRY_DAYS = 5
DAYS_PER_YEAR = 365.0


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annual rate r with sum CF_i / (1 + r)^((t_i - t_0) / 365) = 0.

    Flows from the investor's side: money put in is negative, the final
    value positive. ``None`` when there is no sign change or no root.
    """
    flows = [(d, a) for d, a in flows if a != 0]
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** ((d - t0).days / DAYS_PER_YEAR) for d, a in flows)

    lo, hi = -0.9999, 10.0
    try:
        if npv(lo) * npv(hi) > 0:
            hi = 1000.0
            if npv(lo) * npv(hi) > 0:
                return None
        root = brentq(npv, lo, hi, xtol=1e-10, maxiter=500)
        return float(root)  # type: ignore[arg-type]
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def _period_rate(annual: float | None, days: int) -> float | None:
    if annual is None:
        return None
    return (1.0 + annual) ** (days / DAYS_PER_YEAR) - 1.0


def _price_lookup(series: dict[str, float]):
    keys = sorted(series)

    def at(day: str) -> float | None:
        i = bisect_right(keys, day) - 1
        if i < 0:
            return None
        if (date.fromisoformat(day) - date.fromisoformat(keys[i])).days > MAX_PRICE_CARRY_DAYS:
            return None
        return series[keys[i]]

    return keys, at


def _load_holdings(db: Session, user_id: str, start: date | None):
    """({day: {isin: qty}}, {isin: meta}, {day: {isin: broker price}}) from the snapshots."""
    from app.foundation.models.entities import BookPositionSnapshot

    query = db.query(BookPositionSnapshot).filter(BookPositionSnapshot.user_id == user_id)
    if start is not None:
        query = query.filter(BookPositionSnapshot.snapshot_date >= start - timedelta(days=MAX_PRICE_CARRY_DAYS))
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    broker_price: dict[str, dict[str, float]] = defaultdict(dict)
    meta: dict[str, dict[str, Any]] = {}
    for row in query.order_by(BookPositionSnapshot.snapshot_date).all():
        day = row.snapshot_date.isoformat()
        isin = (row.isin or "").upper()
        qty = float(row.quantity or 0)
        by_day[day][isin] += qty
        m = meta.setdefault(isin, {"isin": isin, "ticker": None, "name": row.name, "sources": set()})
        m["ticker"] = row.ticker or m["ticker"]
        m["sources"].add(row.source)
        price = None
        if row.current_value is not None and qty > 0 and float(row.current_value) > 0:
            price = float(row.current_value) / qty
        elif row.current_price is not None and float(row.current_price) > 0:
            price = float(row.current_price)
        if price and (row.currency or "EUR").upper() == "EUR":
            broker_price[isin][day] = price
    return {d: dict(v) for d, v in by_day.items()}, meta, broker_price


def _dividends_by_day(db: Session, user_id: str, start: str) -> dict[str, float]:
    from app.foundation.models.entities import TaxLedgerEvent

    out: dict[str, float] = defaultdict(float)
    for ev in (
        db.query(TaxLedgerEvent)
        .filter(TaxLedgerEvent.user_id == user_id, TaxLedgerEvent.event_type == "dividend")
        .all()
    ):
        day = ev.event_date.isoformat()
        if day >= start:
            out[day] += float(ev.gross_eur or 0)
    return dict(out)


def compute_book_performance(
    db: Session, user_id: str, *, benchmark: str | None = None, start: date | None = None,
) -> dict[str, Any]:
    """Unit value, value, flows, TWR, IRR and a rebased EUR benchmark of the book."""
    from app.foundation.eur_prices import benchmark_ticker, eur_closes
    from app.foundation.portfolio.isin_resolver import _try_resolve_isin

    holdings, meta, broker_price = _load_holdings(db, user_id, start)
    base: dict[str, Any] = {"available": False, "currency": "EUR", "series": [], "estimate": True}
    if len(holdings) < 1:
        return {**base, "reason": "No position snapshots yet. They are taken after every sync and at 20:00 UTC."}
    snap_days = sorted(holdings)
    first = max(snap_days[0], start.isoformat()) if start else snap_days[0]
    today = date.today().isoformat()

    rate_cache: dict[str, dict[str, float] | None] = {}
    span_days = (date.today() - date.fromisoformat(first)).days + 15
    lookups: dict[str, Any] = {}
    calendar: set[str] = set()
    unpriced: list[str] = []
    for isin, m in meta.items():
        ticker = m["ticker"] or _try_resolve_isin(isin, db)
        m["ticker"] = ticker
        closes = eur_closes(db, ticker, days=span_days, rate_cache=rate_cache) if ticker else {}
        series = {d: p for d, p in closes.items() if d >= first}
        # The broker's own EUR price fills days without a market close.
        for d, p in broker_price.get(isin, {}).items():
            if d >= first:
                series.setdefault(d, p)
        if not series:
            unpriced.append(isin)
            continue
        calendar.update(series)
        lookups[isin] = _price_lookup(series)[1]
    calendar.update(d for d in snap_days if d >= first)
    days = sorted(d for d in calendar if first <= d <= today)
    if len(days) < 2:
        return {**base, "reason": "Fewer than two priced days since the first snapshot.", "unpriced": unpriced}

    def held_on(day: str) -> dict[str, float]:
        i = bisect_right(snap_days, day) - 1
        return holdings[snap_days[i]] if i >= 0 else {}

    def value(qty: dict[str, float], day: str) -> tuple[float, bool]:
        total, complete = 0.0, True
        for isin, q in qty.items():
            if q == 0 or isin not in lookups:
                continue
            p = lookups[isin](day)
            if p is None:
                complete = False
                continue
            total += q * p
        return total, complete

    dividends = _dividends_by_day(db, user_id, first)
    bench_symbol = (benchmark or benchmark_ticker(db)).upper()
    bench_closes = eur_closes(db, bench_symbol, days=span_days, rate_cache=rate_cache)
    bench_at = _price_lookup(bench_closes)[1] if bench_closes else (lambda _day: None)

    unit = 100.0
    q_prev = held_on(days[0])
    v_prev, _ = value(q_prev, days[0])
    b0 = bench_at(days[0])
    series = [{"date": days[0], "unit_value": unit, "value": round(v_prev, 2), "net_flow": 0.0,
               "benchmark": 100.0 if b0 else None}]
    irr_flows: list[tuple[date, float]] = [(date.fromisoformat(days[0]), -v_prev)]
    contributions = 0.0
    gaps = 0
    for day in days[1:]:
        gross, complete = value(q_prev, day)
        _, prev_complete = value(q_prev, series[-1]["date"])
        div = dividends.get(day, 0.0)
        q_now = held_on(day)
        v_now, _ = value(q_now, day)
        if v_prev > 0 and complete and prev_complete:
            unit *= (gross + div) / v_prev
        else:
            gaps += 1
        flow = v_now - gross - div
        contributions += flow
        if abs(flow) > 0.005:
            irr_flows.append((date.fromisoformat(day), -flow))
        if div:
            irr_flows.append((date.fromisoformat(day), div))
        b = bench_at(day)
        series.append({
            "date": day, "unit_value": round(unit, 6), "value": round(v_now, 2), "net_flow": round(flow, 2),
            "benchmark": round(100.0 * b / b0, 6) if (b and b0) else None,
        })
        q_prev, v_prev = q_now, v_now

    end = date.fromisoformat(days[-1])
    begin = date.fromisoformat(days[0])
    span = (end - begin).days
    irr_flows.append((end, v_prev))
    irr = xirr(irr_flows)
    twr = unit / 100.0 - 1.0
    bench_total = (series[-1]["benchmark"] / 100.0 - 1.0) if series[-1]["benchmark"] else None
    annualise = span >= DAYS_PER_YEAR
    return {
        **base,
        "available": True,
        "start": days[0],
        "end": days[-1],
        "days": span,
        "benchmark": bench_symbol,
        "series": series,
        "value_start_eur": series[0]["value"],
        "value_end_eur": round(v_prev, 2),
        "net_contributions_eur": round(contributions, 2),
        "twr": twr,
        "twr_annualised": (1.0 + twr) ** (DAYS_PER_YEAR / span) - 1.0 if annualise and span > 0 else None,
        "benchmark_return": bench_total,
        "benchmark_annualised": (
            (1.0 + bench_total) ** (DAYS_PER_YEAR / span) - 1.0 if annualise and bench_total is not None else None
        ),
        "irr_annualised": irr if annualise else None,
        "irr_period": _period_rate(irr, span),
        "unpriced": unpriced,
        "days_without_full_prices": gaps,
        "method": "Daily time-weighted unit value (flows at the close), EUR; IRR = XIRR of the same flows.",
    }


def unit_value_returns(perf: dict[str, Any]) -> tuple[list[float], list[str]]:
    """Daily returns of the unit value, for risk statistics on the real book."""
    series = perf.get("series") or []
    out: list[float] = []
    dates: list[str] = []
    for prev, cur in zip(series, series[1:]):
        u0, u1 = prev["unit_value"], cur["unit_value"]
        if u0 and math.isfinite(u1 / u0):
            out.append(u1 / u0 - 1.0)
            dates.append(cur["date"])
    return out, dates
