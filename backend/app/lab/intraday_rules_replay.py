"""Replay of intraday stop/target/trailing/regime rules on 5-minute bars (research only, never a pass test).

Frozen on 2026-11-25 before any data was read. Entry: the owner's own chart signal, EMA 9 crossing EMA 26 on 5-minute
closes (long on a cross up, short on a cross down), entered at the next bar's open, between 10:00 and 15:30 New York,
at most 3 entries a day, everything flat at 15:55. Six cells (five rules and a control):

* ``fixed``: stop 1 x ATR(14) of the 5-minute bars, no target, exit on the opposite cross or at 15:55;
* ``tp1_5``: stop 2 x ATR, target 1.5 x the stop distance;
* ``trail``: a trailing stop 2 x ATR behind the best price;
* ``tp1_5_hv`` and ``trail_hv``: the last two, only on days whose trailing 20-day volatility is in the top tercile;
* ``control``: ``tp1_5`` with a coin-flip direction (seeded), to show whether the cross carries anything.

Costs are in basis points of the position's value per round trip (``spread_bp``). The owner's Trading 212 export shows a
median about 7 bp on Tech 100; the desk's registered figure is 0.6 bp; both are reported. Five tests, Holm-adjusted.
Changing a parameter after seeing a result makes it a new trial. A pass would justify a new pre-registration, never a
live signal.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.foundation import technical

NEW_YORK = ZoneInfo("America/New_York")
OPEN, FIRST_ENTRY, LAST_ENTRY, FLAT = time(9, 30), time(10, 0), time(15, 30), time(15, 55)
MAX_ENTRIES = 3
ATR_N = 14
CELLS = ("fixed", "tp1_5", "trail", "tp1_5_hv", "trail_hv", "control")
TESTED = CELLS[:5]
SPREADS_BP = (0.6, 7.0)


@dataclass(frozen=True)
class Bar5:
    t: datetime  # New York local
    o: float
    h: float
    lo: float
    c: float


def sessions(rows: list[dict[str, Any]]) -> dict[date, list[Bar5]]:
    """Regular-session 5-minute bars grouped by New York date (rows: ``{"t", "o", "h", "l", "c"}``)."""
    out: dict[date, list[Bar5]] = defaultdict(list)
    for r in rows:
        t = r["t"].astimezone(NEW_YORK)
        if OPEN <= t.time() < time(16, 0):
            out[t.date()].append(Bar5(t, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])))
    return {d: sorted(v, key=lambda b: b.t) for d, v in out.items() if len(v) >= 70}


def day_vol(sess: dict[date, list[Bar5]]) -> dict[date, float | None]:
    """Trailing 20-day realised volatility of daily closes, known before each day."""
    days = sorted(sess)
    closes = [sess[d][-1].c for d in days]
    out: dict[date, float | None] = {}
    for i, d in enumerate(days):
        w = closes[max(0, i - 20):i]
        rets = [math.log(b / a) for a, b in zip(w, w[1:], strict=False)]
        out[d] = statistics.pstdev(rets) if len(rets) >= 19 else None
    return out


def _atr(bars: list[Bar5], i: int) -> float | None:
    if i < ATR_N:
        return None
    trs = [max(b.h - b.lo, abs(b.h - p.c), abs(b.lo - p.c)) for p, b in zip(bars[i - ATR_N:i], bars[i - ATR_N + 1:i + 1], strict=False)]
    return sum(trs) / len(trs)


def _crosses(bars: list[Bar5]) -> dict[int, str]:
    closes = [b.c for b in bars]
    e9, e26 = technical.ema_series(closes, 9), technical.ema_series(closes, 26)
    # e9 starts at bar index 8 and e26 at 25, so bar i is e9[i - 8] and e26[i - 25]
    out: dict[int, str] = {}
    for i in range(26, len(bars)):
        a, b = e9[i - 8] - e26[i - 25], e9[i - 1 - 8] - e26[i - 1 - 25]
        if a > 0 >= b:
            out[i] = "long"
        elif a < 0 <= b:
            out[i] = "short"
    return out


def simulate_day(bars: list[Bar5], cell: str, rng: random.Random) -> list[tuple[float, str]]:
    """Gross returns (fraction of the entry price) and the exit reason, per trade of one day."""
    crosses = _crosses(bars)
    base = cell.replace("_hv", "")
    stop_k, target_m, trail = (1.0, None, False) if base == "fixed" else (2.0, 1.5, False) if base in ("tp1_5", "control") else (2.0, None, True)
    out: list[tuple[float, str]] = []
    pos: dict[str, Any] | None = None
    entries = 0
    for i in range(26, len(bars) - 1):
        b = bars[i]
        if pos is not None:
            d, px = pos["d"], pos["px"]
            hit: tuple[float, str] | None = None
            if (d == "long" and b.lo <= pos["stop"]) or (d == "short" and b.h >= pos["stop"]):
                hit = (pos["stop"], "stop")
            elif pos["target"] is not None and ((d == "long" and b.h >= pos["target"]) or (d == "short" and b.lo <= pos["target"])):
                hit = (pos["target"], "target")
            elif b.t.time() >= FLAT:
                hit = (b.c, "flat")
            elif trail is False and base == "fixed" and i in crosses and crosses[i] != d:
                hit = (b.c, "cross")
            if hit:
                sign = 1 if d == "long" else -1
                out.append((sign * (hit[0] / px - 1), hit[1]))
                pos = None
            elif trail:
                if d == "long":
                    pos["best"] = max(pos["best"], b.h)
                    pos["stop"] = max(pos["stop"], pos["best"] - pos["dist"])
                else:
                    pos["best"] = min(pos["best"], b.lo)
                    pos["stop"] = min(pos["stop"], pos["best"] + pos["dist"])
        if pos is None and i in crosses and FIRST_ENTRY <= b.t.time() <= LAST_ENTRY and entries < MAX_ENTRIES:
            a = _atr(bars, i)
            if not a:
                continue
            d = crosses[i] if cell != "control" else rng.choice(("long", "short"))
            px = bars[i + 1].o
            dist = stop_k * a
            pos = {"d": d, "px": px, "dist": dist, "best": px, "stop": px - dist if d == "long" else px + dist,
                   "target": None if target_m is None else (px + target_m * dist if d == "long" else px - target_m * dist)}
            entries += 1
    if pos is not None:
        sign = 1 if pos["d"] == "long" else -1
        out.append((sign * (bars[-1].c / pos["px"] - 1), "flat"))
    return out


def evaluate(sess: dict[date, list[Bar5]], spread_bp: float, seed: int = 7) -> dict[str, Any]:
    """Per cell: n, mean net bp, t, hit rate and the exit mix; Holm-adjusted p over the five tested cells."""
    from scipy import stats as st

    vol = day_vol(sess)
    known = sorted(v for v in vol.values() if v is not None)
    cut = known[int(len(known) * 2 / 3)] if len(known) >= 30 else math.inf
    res: dict[str, Any] = {}
    p_raw: dict[str, float] = {}
    for cell in CELLS:
        rng = random.Random(seed)
        nets: list[float] = []
        reasons: dict[str, int] = defaultdict(int)
        for d in sorted(sess):
            if cell.endswith("_hv") and not ((vol[d] or 0) >= cut):
                continue
            for g, why in simulate_day(sess[d], cell, rng):
                nets.append(g * 1e4 - spread_bp)
                reasons[why] += 1
        n = len(nets)
        sd = statistics.stdev(nets) if n > 1 else 0.0
        t = statistics.fmean(nets) / (sd / math.sqrt(n)) if n > 1 and sd > 0 else None
        res[cell] = {"n": n, "mean_net_bp": statistics.fmean(nets) if nets else None, "t": t,
                     "hit_rate": sum(1 for x in nets if x > 0) / n if n else None, "exits": dict(reasons)}
        if cell in TESTED and t is not None and n > 2:
            p_raw[cell] = float(2 * st.t.sf(abs(t), n - 1))
    running = 0.0
    for i, k in enumerate(sorted(p_raw, key=p_raw.__getitem__)):
        running = max(running, min(1.0, (len(p_raw) - i) * p_raw[k]))
        res[k]["p_holm"] = running
    return res


def run(rows_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sym, rows in rows_by_symbol.items():
        sess = sessions(rows)
        out[sym] = {"days": len(sess), **{f"spread_{s}bp": evaluate(sess, s) for s in SPREADS_BP}}
    return out
