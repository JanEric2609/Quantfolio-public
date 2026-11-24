"""Pure technical indicators and the event desk's rules (ADR 0028). No I/O: the desk and the lab replay share it.

``EVENT_RULES`` is registered in ``decision/desk/prereg_events.py``; changing a parameter moves its hash.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

#: Every rule parameter of the event desk's registration (textbook values, never tuned).
EVENT_RULES: dict[str, Any] = {
    "ta_trend": {"ema_fast": 20, "ema_slow": 50, "stop_atr": 2.0, "max_hold_days": 10,
                 "exit": "opposite cross"},
    "ta_band": {"sma": 20, "stdev": 2.0, "stop_atr": 1.5, "max_hold_days": 5, "exit": "close back at the middle band"},
    "ta_rsi2": {"rsi": 2, "low": 10, "high": 90, "exit_level": 50, "trend_sma": 200, "stop_atr": 1.5,
                "max_hold_days": 5},
    "atr": 14,
    "evaluation": {"XETR": "17:10 Berlin", "XNYS": "21:40 Berlin"},
    "hype_fade": {"direction": "short", "min_day_change": 0.08, "min_price_usd": 5, "volume_multiple": 3,
                  "news_min_count": 5, "news_multiple": 3, "baseline_days": 20, "stop_atr": 1.5,
                  "max_hold_days": 10, "window_ny": "10:30-15:30", "every_minutes": 15,
                  "no_primary_source": ["sec_8k", "de_adhoc", "earnings headline"]},
    "news_drift": {"min_materiality": 3, "scope": "company", "sources": ["sec_8k", "de_adhoc", "earnings headline"],
                   "min_age_minutes": 30, "move_sigmas": 2, "sigma_days": 20, "stop_atr": 1.5, "max_hold_days": 5},
    "owner_call": {"default_stop_atr": 1.5, "default_hold": "5d"},
}


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class RuleHit:
    cell: str
    direction: str  # long | short
    stop_atr: float
    max_hold_days: int
    reason: str


def bars_from_rows(rows: list[dict[str, Any]]) -> list[Bar]:
    out: dict[date, Bar] = {}
    for r in rows:
        try:
            d = date.fromisoformat(str(r["date"])[:10])
            close = float(r["close"])
            out[d] = Bar(d, float(r.get("open") or close), float(r.get("high") or close),
                         float(r.get("low") or close), close)
        except (KeyError, TypeError, ValueError):
            continue
    return [out[d] for d in sorted(out)]


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def ema_series(values: list[float], n: int) -> list[float]:
    """EMA seeded with the first n values' mean; the list starts at index n-1 of *values*."""
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    cur = sum(values[:n]) / n
    out = [cur]
    for v in values[n:]:
        cur = v * k + cur * (1 - k)
        out.append(cur)
    return out


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    """(lower, middle, upper) from the last n closes (population stdev)."""
    if len(values) < n:
        return None
    window = values[-n:]
    mid = sum(window) / n
    sd = math.sqrt(sum((v - mid) ** 2 for v in window) / n)
    return mid - k * sd, mid, mid + k * sd


def rsi(values: list[float], n: int = 2) -> float | None:
    """Wilder's RSI(n), seeded with the first n changes."""
    if len(values) < n + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(c, 0) for c in changes[:n]) / n
    loss = sum(max(-c, 0) for c in changes[:n]) / n
    for c in changes[n:]:
        gain = (gain * (n - 1) + max(c, 0)) / n
        loss = (loss * (n - 1) + max(-c, 0)) / n
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def atr(bars: list[Bar], n: int = 14) -> float | None:
    """Mean true range of the last n bars."""
    if len(bars) < n + 1:
        return None
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars, bars[1:], strict=False)]
    return sum(trs[-n:]) / n


def rule_hits(bars: list[Bar], rules: dict[str, Any] | None = None) -> list[RuleHit]:
    """Entry signals on the last bar (which may be provisional)."""
    rules = rules or EVENT_RULES
    closes = [b.close for b in bars]
    hits: list[RuleHit] = []
    t = rules["ta_trend"]
    fast, slow = ema_series(closes, t["ema_fast"]), ema_series(closes, t["ema_slow"])
    if len(slow) >= 2:
        # Align the two series on the same bars: fast has len(closes)-fast_n+1 values.
        f_now, f_prev = fast[-1], fast[-2]
        s_now, s_prev = slow[-1], slow[-2]
        if f_prev <= s_prev and f_now > s_now:
            hits.append(RuleHit("ta_trend:cfd", "long", t["stop_atr"], t["max_hold_days"], "EMA 20 crossed above EMA 50"))
        elif f_prev >= s_prev and f_now < s_now:
            hits.append(RuleHit("ta_trend:cfd", "short", t["stop_atr"], t["max_hold_days"], "EMA 20 crossed below EMA 50"))
    b = rules["ta_band"]
    band = bollinger(closes, b["sma"], b["stdev"])
    if band:
        lo, _mid, hi = band
        if closes[-1] < lo:
            hits.append(RuleHit("ta_band:cfd", "long", b["stop_atr"], b["max_hold_days"], "close below the lower Bollinger band"))
        elif closes[-1] > hi:
            hits.append(RuleHit("ta_band:cfd", "short", b["stop_atr"], b["max_hold_days"], "close above the upper Bollinger band"))
    r = rules["ta_rsi2"]
    trend = sma(closes, r["trend_sma"])
    val = rsi(closes, r["rsi"])
    if trend is not None and val is not None:
        if val < r["low"] and closes[-1] > trend:
            hits.append(RuleHit("ta_rsi2:cfd", "long", r["stop_atr"], r["max_hold_days"], f"RSI(2) {val:.0f}, above the 200-day average"))
        elif val > r["high"] and closes[-1] < trend:
            hits.append(RuleHit("ta_rsi2:cfd", "short", r["stop_atr"], r["max_hold_days"], f"RSI(2) {val:.0f}, below the 200-day average"))
    return hits


def exit_hit(cell: str, direction: str, bars: list[Bar], rules: dict[str, Any] | None = None) -> str | None:
    """The rule's own exit on the last bar: an opposite EMA cross, a close back at the middle band, RSI(2) across 50."""
    rules = rules or EVENT_RULES
    closes = [b.close for b in bars]
    if cell == "ta_trend:cfd":
        t = rules["ta_trend"]
        fast, slow = ema_series(closes, t["ema_fast"]), ema_series(closes, t["ema_slow"])
        if len(slow) >= 2:
            if direction == "long" and fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
                return "opposite_cross"
            if direction == "short" and fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
                return "opposite_cross"
    elif cell == "ta_band:cfd":
        b = rules["ta_band"]
        band = bollinger(closes, b["sma"], b["stdev"])
        if band:
            mid = band[1]
            if (direction == "long" and closes[-1] >= mid) or (direction == "short" and closes[-1] <= mid):
                return "middle_band"
    elif cell == "ta_rsi2:cfd":
        r = rules["ta_rsi2"]
        val = rsi(closes, r["rsi"])
        if val is not None and ((direction == "long" and val > r["exit_level"])
                                or (direction == "short" and val < r["exit_level"])):
            return "rsi_50"
    return None
