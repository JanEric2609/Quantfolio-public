"""The algorithmic-trading section's candidate list as plain data (ADR 0029 Amendment 1).

Mirrors ``docs/plan-algo-strategy-cards-2026-11-25.md``. Nothing here is registered, funded or
scored: the cards are drafts until the owner approves a start date. Pure data, no database.
"""
from __future__ import annotations

from typing import Any

STATUS = "research_only"
FAMILY = "slow_etf"
MAX_ARMS = 3

CARDS: list[dict[str, Any]] = [
    {"id": "sma10_switch", "name": "10-month moving-average switch", "quantpedia": "#0001 Asset Class Trend-Following",
     "verdict_class": "risk overlay", "turnover": "3-4 round trips a year in total",
     "summary": "Hold each sleeve while its month-end close is above its 10-month average, else cash.",
     "prior": "No reliable excess return expected; the possible value is a smaller drawdown."},
    {"id": "tsmom12_volscale", "name": "12-month trend, volatility-scaled", "quantpedia": "Time Series Momentum (#0118)",
     "verdict_class": "risk overlay", "turnover": "monthly rescaling",
     "summary": "Weight 0 to 1 from the sign of the 12-month return, scaled to a 10 % volatility target.",
     "prior": "Out-of-sample evidence for volatility scaling is negative; this arm exists to measure it."},
    {"id": "sma10_switch_bond", "name": "Moving-average switch into bonds", "quantpedia": "Paired Switching",
     "verdict_class": "risk overlay", "turnover": "3-4 round trips a year in total",
     "summary": "As the first card, with an aggregate-bond UCITS ETF instead of cash as the exit leg.",
     "prior": "Same family and evidence as the first card."},
]

NOT_CARDED: list[dict[str, str]] = [
    {"quantpedia": "#0002 Momentum Asset Allocation", "why": "Top 3 of 5 by 12-month return; reported drawdown -47.8 % "
     "is not a risk overlay, so it would need the alpha gate. Same family, which is capped at three arms."},
    {"quantpedia": "#0005 FX Carry", "why": "Needs currency forwards or CFDs: leverage, financing and no UCITS route. "
     "Not a slow long-only ETF rule."},
    {"quantpedia": "#0008 Currency Momentum", "why": "Same instrument problem as FX carry, plus monthly turnover in a small pot."},
    {"quantpedia": "#0009 Currency Value (PPP)", "why": "Same instrument problem; a multi-year signal that cannot be verified in our horizon."},
    {"quantpedia": "#0021 Commodity Momentum", "why": "Needs commodity futures; the UCITS commodity ETFs carry roll yield that the paper does not model."},
    {"quantpedia": "#0031 Equity Seasonality", "why": "Quantpedia rates it weak with a negative out-of-sample test of its own; watch item, not registered."},
]

# Paper cells: pre-registered rules scored on paper only, outside the slow-ETF family cap. Nothing is funded.
PAPER_CELLS: list[dict[str, Any]] = [
    {"id": "trend100_state", "name": "Slow trend with a state exit on indices, commodities and FX",
     "rule": "Enter on a 100-day high or low close; exit when the close crosses back through the 50-day EMA or a 5 ATR stop "
             "is hit. No fixed holding time.",
     "instruments": "Tech 100, S&P 500, DAX, EU 50; gold, silver, WTI, Brent, copper; six USD pairs (daily bars)",
     "verdict_class": "alpha candidate",
     "evidence": "Best-evidenced family in the research (diversified futures, decades); thin on a few CFDs, which cost "
                 "several times a futures contract.",
     "status": "replay written, not yet run on real prices",
     "test": "scripts/trend_replay.py: six declared trials, Holm, side placebo, quarter-block bootstrap; null unless "
             "every part of the frozen rule is met."},
]
