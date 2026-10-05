"""Transaction-cost model for the advisor paper sleeve.

The paper portfolio executed at the exact Monte-Carlo spot price with no
commission, which made churn free. A model that pays nothing to trade has no
reason not to trade, and every scorecard axis downstream inherits the
optimism: NAV, Sharpe, and ultimately the graduation decision.

Defaults mirror DKB Broker's published domestic equity schedule (2026):

===================  =====
Order volume         Fee
===================  =====
up to €5,000         €10
€5,000.01 – €20,000  €15
above €20,000        €30
===================  =====

An additional €2.50 Ausführungsentgelt applies on Xetra and the floor
exchanges but not on Tradegate, gettex, Quotrix or OTC, so ``venue_fee``
defaults to 0 — the sleeve assumes the cheap venue. Override per strategy via
``AdvisorStrategy.config_json["fee_schedule"]``.

Fees are charged on both sides: a buy costs ``value + fee`` in cash, a sell
returns ``value - fee``.
"""
from __future__ import annotations

import math
from typing import Any

# (upper_bound_inclusive, fee) ordered ascending; anything above the last
# bound pays ``above``.
DEFAULT_FEE_SCHEDULE: dict[str, Any] = {
    "tiers": [(5_000.0, 10.0), (20_000.0, 15.0)],
    "above": 30.0,
    "venue_fee": 0.0,
    "currency": "EUR",
}


def resolve_fee_schedule(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a strategy's ``fee_schedule`` override over the defaults."""
    override = (config or {}).get("fee_schedule")
    if not isinstance(override, dict):
        return dict(DEFAULT_FEE_SCHEDULE)
    schedule = {**DEFAULT_FEE_SCHEDULE, **override}
    raw_tiers = schedule.get("tiers")
    if isinstance(raw_tiers, list):
        tiers: list[tuple[float, float]] = []
        for tier in raw_tiers:
            # JSON round-trips tuples as lists.
            if isinstance(tier, (list, tuple)) and len(tier) == 2:
                try:
                    tiers.append((float(tier[0]), float(tier[1])))
                except (TypeError, ValueError):
                    continue
        schedule["tiers"] = sorted(tiers) if tiers else list(DEFAULT_FEE_SCHEDULE["tiers"])
    else:
        schedule["tiers"] = list(DEFAULT_FEE_SCHEDULE["tiers"])
    return schedule


def trade_fee(notional: float, schedule: dict[str, Any] | None = None) -> float:
    """Commission for a single order of *notional* euros.

    Args:
        notional: Absolute order value (price * quantity), before fees.
        schedule: Fee schedule; defaults to :data:`DEFAULT_FEE_SCHEDULE`.

    Returns:
        Fee in euros. Zero for a zero/negative notional so a no-op trade is
        never charged.
    """
    if notional <= 0:
        return 0.0
    sched = schedule if schedule is not None else DEFAULT_FEE_SCHEDULE
    tiers = sched.get("tiers") or DEFAULT_FEE_SCHEDULE["tiers"]
    fee = float(sched.get("above", DEFAULT_FEE_SCHEDULE["above"]))
    for upper, tier_fee in tiers:
        if notional <= float(upper):
            fee = float(tier_fee)
            break
    return round(fee + float(sched.get("venue_fee", 0.0) or 0.0), 2)


def affordable_notional(cash: float, schedule: dict[str, Any] | None = None) -> float:
    """Largest order value *cash* can pay for once commission is added.

    ``notional + trade_fee(notional)`` rises with notional but steps at every
    tier boundary, so the answer is the best of "cash net of that tier's fee,
    clipped into that tier" across all of them.

    Used to right-size a buy the sleeve cannot fully afford: without it a buy
    financed by a funding sell is rejected outright whenever the sell came up
    even a few euros short, leaving the sleeve sitting in cash it just raised.

    Args:
        cash: Available balance in euros.
        schedule: Fee schedule; defaults to :data:`DEFAULT_FEE_SCHEDULE`.

    Returns:
        Notional in euros, floored to whole cents so ``notional + fee`` never
        rounds back above *cash*. ``0.0`` when no order is affordable.
    """
    if cash <= 0:
        return 0.0
    sched = schedule if schedule is not None else DEFAULT_FEE_SCHEDULE
    tiers = sched.get("tiers") or DEFAULT_FEE_SCHEDULE["tiers"]
    venue = float(sched.get("venue_fee", 0.0) or 0.0)

    bands: list[tuple[float, float, float]] = []
    lower = 0.0
    for upper, tier_fee in tiers:
        bands.append((lower, float(upper), float(tier_fee)))
        lower = float(upper)
    bands.append((lower, float("inf"), float(sched.get("above", DEFAULT_FEE_SCHEDULE["above"]))))

    best = 0.0
    for band_lower, band_upper, tier_fee in bands:
        candidate = min(cash - round(tier_fee + venue, 2), band_upper)
        if candidate > band_lower and candidate > best:
            best = candidate
    if best <= 0:
        return 0.0
    return math.floor(best * 100) / 100


def describe_fee_schedule(schedule: dict[str, Any] | None = None) -> str:
    """One-line human/LLM-readable rendering of the schedule."""
    sched = schedule if schedule is not None else DEFAULT_FEE_SCHEDULE
    tiers = sched.get("tiers") or DEFAULT_FEE_SCHEDULE["tiers"]
    parts = []
    lower = 0.0
    for upper, tier_fee in tiers:
        parts.append(f"€{lower:,.0f}–€{float(upper):,.0f}: €{float(tier_fee):,.2f}")
        lower = float(upper)
    parts.append(f"above €{lower:,.0f}: €{float(sched.get('above', 0.0)):,.2f}")
    venue = float(sched.get("venue_fee", 0.0) or 0.0)
    if venue:
        parts.append(f"plus €{venue:,.2f} venue fee per order")
    return "; ".join(parts)
