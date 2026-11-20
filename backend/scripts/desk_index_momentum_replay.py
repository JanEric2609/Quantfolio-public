"""Early replay of the desk's index rule on SPY and QQQ (ADR 0027, plan WP7). Informational only.

    DATABASE_URL=... .venv/bin/python scripts/desk_index_momentum_replay.py [--years 6]

The Gao, Han, Li, Zhou rule on Alpaca IEX 30-minute bars: the sign of the return from the previous close to the
first half hour is the direction of the last half hour. The product is modelled as 5 x the underlying return minus
a round-trip cost of ``spread + 2 x 0.99 EUR / notional``, the notional from the desk's sizing rule. It is recorded as
``record_trial(db, "desk_replay", ...)`` and never feeds the pre-registered pass rule.
"""
from __future__ import annotations

import argparse
import math
import statistics
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
LEVERAGE = Decimal(5)
SPREAD = Decimal("0.005")  # an assumed round-trip spread share of notional: the replay has no product quotes
BUDGET = Decimal("5") - 2 * Decimal("0.99")
FEES = 2 * Decimal("0.99")


def replay(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per trading day: ``{"day", "direction", "net_eur"}``. *bars* are 30-minute bars with ``t`` (UTC), ``o``, ``c``."""
    by_day: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for b in bars:
        local = b["t"].astimezone(NY)
        by_day[local.date()].append({**b, "local": local})
    days = sorted(by_day)
    closes: list[float] = []
    out: list[dict[str, Any]] = []
    prev_close: Decimal | None = None
    for day in days:
        rows = sorted(by_day[day], key=lambda r: r["t"])
        first = next((r for r in rows if r["local"].time() == time(9, 30)), None)
        last = next((r for r in rows if r["local"].time() == time(15, 30)), None)
        day_close = rows[-1]["c"]
        if prev_close and first and last and len(rows) >= 12 and first["c"] and last["o"] and last["c"]:
            r1 = first["c"] / prev_close - 1
            if r1 != 0 and len(closes) >= 21:
                rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes))]
                sigma = Decimal(str(statistics.stdev(rets)))
                fraction = min(max(LEVERAGE * sigma, Decimal("0.03")), Decimal("0.30"))
                notional = BUDGET / fraction
                direction = 1 if r1 > 0 else -1
                r_last = last["c"] / last["o"] - 1
                net = notional * (LEVERAGE * direction * r_last - SPREAD) - FEES
                out.append({"day": day, "direction": direction, "net_eur": float(net)})
        prev_close = day_close
        closes.append(float(day_close))
    return out


def summarise(rows: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    by_year: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        by_year[r["day"].year].append(r["net_eur"])
    out: dict[int, dict[str, float]] = {}
    for year, nets in sorted(by_year.items()):
        mean = statistics.fmean(nets)
        sd = statistics.stdev(nets) if len(nets) > 1 else 0.0
        out[year] = {"n": len(nets), "mean_net": mean, "t": mean / (sd / math.sqrt(len(nets))) if sd else 0.0,
                     "hit_rate": sum(1 for x in nets if x > 0) / len(nets)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=6)
    args = parser.parse_args()
    from app.decision.desk.news import alpaca_for
    from app.foundation.core.db import SessionLocal
    from app.foundation.quant_metrics import record_trial

    db = SessionLocal()
    try:
        provider = alpaca_for(db)
        if provider is None:
            raise SystemExit("No Alpaca key stored.")
        end = datetime.now(UTC)
        start = end - timedelta(days=365 * args.years)
        for symbol in ("SPY", "QQQ"):
            rows = replay(provider.get_intraday_bars(symbol, "30Min", start, end))
            print(symbol, "days traded:", len(rows))
            for year, stats in summarise(rows).items():
                print(f"  {year}: n={stats['n']:.0f} mean={stats['mean_net']:.2f} EUR t={stats['t']:.2f} hit={stats['hit_rate']:.0%}")
            record_trial(db, "desk_replay", f"{symbol}:v1", {"informational": True, "days": len(rows)})
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
