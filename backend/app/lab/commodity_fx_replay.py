"""Strong-day ride on commodity and FX daily bars: the shock-ride replay with commodity and FX cost tables (research only).

The engine, constants and decision rule are those of ``shock_ride_replay`` (no clock exit, chandelier and EMA exits,
block-bootstrap p, side placebo as the required control). This module adds only what differs by asset class:

* the instrument table (daily-bar ticker, spread, overnight financing, USD-result flag),
* the implied-volatility index per instrument where one exists (``^GVZ`` gold, ``^OVX`` crude, ``^EVZ`` euro),
* two families run separately (commodities, FX), each with its own Holm family and placebo.

Every cost below is an ASSUMPTION, not a published Trading 212 figure: the broker publishes spreads and overnight rates
per instrument inside the app only (the cost documents give worked examples for EUR/USD and crude oil). The numbers are
set at or above those examples. ``stress`` multiplies them so the user can re-run with the real figures read off the
Instrument details page. Continuous futures series (``GC=F``, ``CL=F``) carry unadjusted roll gaps; a gap is a price
jump the replay cannot trade, so read commodity results with that bias in mind.

Writes nothing, sends nothing, calls no broker. A pass justifies a new pre-registration, never a live signal.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.foundation import technical
from app.lab import shock_ride_replay as sr

# key -> daily-bar ticker, assumed costs. spread_bp is the full spread in basis points of price; overnight_* is the
# fraction of notional per night (positive = cost). FX credit on the favourable side is ignored (set to 0 or a cost).
COMMODITIES: dict[str, dict[str, Any]] = {
    "GOLD": {"ticker": "GC=F", "spread_bp": 3.0, "usd": True, "overnight_long": 0.000245, "overnight_short": 0.00005, "vol": "^GVZ"},
    "SILVER": {"ticker": "SI=F", "spread_bp": 8.0, "usd": True, "overnight_long": 0.000245, "overnight_short": 0.00005, "vol": None},
    "WTI": {"ticker": "CL=F", "spread_bp": 5.0, "usd": True, "overnight_long": 0.000245, "overnight_short": 0.00002, "vol": "^OVX"},
    "BRENT": {"ticker": "BZ=F", "spread_bp": 5.0, "usd": True, "overnight_long": 0.000245, "overnight_short": 0.00002, "vol": None},
    "COPPER": {"ticker": "HG=F", "spread_bp": 6.0, "usd": True, "overnight_long": 0.000245, "overnight_short": 0.00005, "vol": None},
}
FX: dict[str, dict[str, Any]] = {
    "EURUSD": {"ticker": "EURUSD=X", "spread_bp": 0.9, "usd": True, "overnight_long": 0.000095, "overnight_short": 0.0, "vol": "^EVZ"},
    "GBPUSD": {"ticker": "GBPUSD=X", "spread_bp": 1.3, "usd": True, "overnight_long": 0.00011, "overnight_short": 0.00011, "vol": None},
    "USDJPY": {"ticker": "JPY=X", "spread_bp": 1.1, "usd": True, "overnight_long": 0.00011, "overnight_short": 0.00011, "vol": None},
    "AUDUSD": {"ticker": "AUDUSD=X", "spread_bp": 1.6, "usd": True, "overnight_long": 0.00011, "overnight_short": 0.00011, "vol": None},
    "USDCHF": {"ticker": "CHF=X", "spread_bp": 1.6, "usd": True, "overnight_long": 0.00011, "overnight_short": 0.00011, "vol": None},
    "USDCAD": {"ticker": "CAD=X", "spread_bp": 1.6, "usd": True, "overnight_long": 0.00011, "overnight_short": 0.00011, "vol": None},
}
FAMILIES = {"commodities": COMMODITIES, "fx": FX}


def stress(table: dict[str, dict[str, Any]], mult: float) -> dict[str, dict[str, Any]]:
    """Spread and overnight rates times ``mult`` (a copy). ``mult`` 2 asks whether anything survives double the costs."""
    if mult <= 0:
        raise ValueError("cost multiplier must be positive")
    out: dict[str, dict[str, Any]] = {}
    for k, v in table.items():
        out[k] = {**v, "spread_bp": v["spread_bp"] * mult, "overnight_long": v["overnight_long"] * mult,
                  "overnight_short": v["overnight_short"] * mult}
    return out


def run_family(table: dict[str, dict[str, Any]], bars_by_key: dict[str, list[technical.Bar]],
               vol_by_key: dict[str, dict[date, float]] | None = None, draws: int = 100) -> dict[str, Any]:
    """One family through the shared engine; instruments without bars are skipped, the implied trigger runs only where a
    volatility index loaded."""
    keys = [k for k in table if k in bars_by_key]
    insts = {k: {f: table[k][f] for f in ("spread_bp", "usd", "overnight_long", "overnight_short")} for k in keys}
    return sr.run({k: bars_by_key[k] for k in keys}, insts, implied_by_key={k: v for k, v in (vol_by_key or {}).items() if k in keys},
                  draws=draws)
