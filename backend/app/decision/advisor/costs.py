"""Transaction-cost model for every paper sleeve.

The paper portfolio executed at the exact Monte-Carlo spot price with no
commission, which made churn free. A model that pays nothing to trade has no
reason not to trade, and every scorecard axis downstream inherits the
optimism: NAV, Sharpe, and ultimately the graduation decision.

New money goes through Scalable Capital (FREE Broker), so every sleeve
(advisor, challenger, mandates A/B, manual) is charged its schedule, checked
2026-10-06 and kept in :mod:`app.foundation.broker_fees` (venue EIX):

* 0.99 EUR per order, and
* 0 EUR for a *buy* of at least 250 EUR in an ETF of a Prime partner
  (iShares, Vanguard, Xtrackers, Amundi), recognised by the instrument name;
  a sale always pays. Without a name the order is assumed to be non-Prime.

``venue_fee`` defaults to 0 and is added on top per order. A strategy can
still override the whole schedule via
``AdvisorStrategy.config_json["fee_schedule"]``: giving ``tiers``/``above``
switches to a volume-tiered schedule (the former DKB one is
``{"tiers": [[5000, 10], [20000, 15]], "above": 30}``).

Fees are charged on both sides: a buy costs ``value + fee`` in cash, a sell
returns ``value - fee``.
"""
from __future__ import annotations

import math
from typing import Any

from app.foundation.broker_fees import (
    DKB_ORDER_ABOVE_EUR,
    DKB_ORDER_TIERS_EUR,
    SCALABLE_ORDER_FEE_EUR,
    SCALABLE_PRIME_MIN_ORDER_EUR,
    is_prime_etf,
    scalable_order_fee,
)

# The default schedule is Scalable's (flat fee + Prime-ETF exemption); it has
# no ``tiers`` key. A schedule with ``tiers`` is volume-tiered: ascending
# (upper_bound_inclusive, fee) pairs, anything above the last bound pays
# ``above``.
DEFAULT_FEE_SCHEDULE: dict[str, Any] = {
    "broker": "scalable",
    "venue_fee": 0.0,
    "currency": "EUR",
}

# Reference tiers for a ``{"broker": "dkb"}`` override.
_DKB_TIERS: list[tuple[float, float]] = list(DKB_ORDER_TIERS_EUR)


def resolve_fee_schedule(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a strategy's ``fee_schedule`` override over the defaults."""
    override = (config or {}).get("fee_schedule")
    if not isinstance(override, dict):
        return dict(DEFAULT_FEE_SCHEDULE)
    schedule = {**DEFAULT_FEE_SCHEDULE, **override}
    if schedule.get("broker") == "dkb" and "tiers" not in override:
        schedule["tiers"] = list(_DKB_TIERS)
        schedule.setdefault("above", DKB_ORDER_ABOVE_EUR)
    if "tiers" in schedule:
        raw_tiers = schedule.get("tiers")
        tiers: list[tuple[float, float]] = []
        if isinstance(raw_tiers, list):
            for tier in raw_tiers:
                # JSON round-trips tuples as lists.
                if isinstance(tier, (list, tuple)) and len(tier) == 2:
                    try:
                        tiers.append((float(tier[0]), float(tier[1])))
                    except (TypeError, ValueError):
                        continue
        # Malformed tiers fall back to the default (Scalable) schedule.
        if tiers:
            schedule["tiers"] = sorted(tiers)
            schedule["above"] = float(schedule.get("above", DKB_ORDER_ABOVE_EUR))
        else:
            schedule.pop("tiers", None)
            schedule.pop("above", None)
    return schedule


def _venue(sched: dict[str, Any]) -> float:
    return float(sched.get("venue_fee", 0.0) or 0.0)


def trade_fee(
    notional: float,
    schedule: dict[str, Any] | None = None,
    name: str | None = None,
    side: str = "buy",
) -> float:
    """Commission for a single order of *notional* euros.

    Args:
        notional: Absolute order value (price * quantity), before fees.
        schedule: Fee schedule; defaults to :data:`DEFAULT_FEE_SCHEDULE`.
        name: Instrument name, used by the Scalable default to recognise a
            Prime ETF (a buy is free from 250 EUR). ``None`` means non-Prime.
        side: ``"buy"`` or ``"sell"``; a Prime ETF sale always pays.

    Returns:
        Fee in euros. Zero for a zero/negative notional so a no-op trade is
        never charged.
    """
    if notional <= 0:
        return 0.0
    sched = schedule if schedule is not None else DEFAULT_FEE_SCHEDULE
    tiers = sched.get("tiers")
    if not tiers:
        return round(scalable_order_fee(notional, name, side) + _venue(sched), 2)
    fee = float(sched.get("above", DKB_ORDER_ABOVE_EUR))
    for upper, tier_fee in tiers:
        if notional <= float(upper):
            fee = float(tier_fee)
            break
    return round(fee + _venue(sched), 2)


def _fee_bands(sched: dict[str, Any], name: str | None) -> list[tuple[float, float, float]]:
    """``(lower_exclusive, upper_inclusive, fee)`` bands of one schedule."""
    venue = _venue(sched)
    tiers = sched.get("tiers")
    if not tiers:
        flat = round(SCALABLE_ORDER_FEE_EUR + venue, 2)
        if is_prime_etf(name):
            return [
                (0.0, SCALABLE_PRIME_MIN_ORDER_EUR, flat),
                (SCALABLE_PRIME_MIN_ORDER_EUR, float("inf"), round(venue, 2)),
            ]
        return [(0.0, float("inf"), flat)]
    bands: list[tuple[float, float, float]] = []
    lower = 0.0
    for upper, tier_fee in tiers:
        bands.append((lower, float(upper), round(float(tier_fee) + venue, 2)))
        lower = float(upper)
    bands.append((lower, float("inf"), round(float(sched.get("above", DKB_ORDER_ABOVE_EUR)) + venue, 2)))
    return bands


def affordable_notional(
    cash: float,
    schedule: dict[str, Any] | None = None,
    name: str | None = None,
) -> float:
    """Largest order value *cash* can pay for once commission is added.

    ``notional + trade_fee(notional)`` rises with notional but steps at every
    fee boundary (the 250 EUR Prime threshold, or each tier), so the answer
    is the best of "cash net of that band's fee, clipped into that band"
    across all of them.

    Used to right-size a buy the sleeve cannot fully afford: without it a buy
    financed by a funding sell is rejected outright whenever the sell came up
    even a few euros short, leaving the sleeve sitting in cash it just raised.

    Args:
        cash: Available balance in euros.
        schedule: Fee schedule; defaults to :data:`DEFAULT_FEE_SCHEDULE`.
        name: Instrument name (Prime-ETF recognition, see :func:`trade_fee`).

    Returns:
        Notional in euros, floored to whole cents so ``notional + fee`` never
        rounds back above *cash*. ``0.0`` when no order is affordable.
    """
    if cash <= 0:
        return 0.0
    sched = schedule if schedule is not None else DEFAULT_FEE_SCHEDULE
    best = 0.0
    for band_lower, band_upper, band_fee in _fee_bands(sched, name):
        candidate = min(cash - band_fee, band_upper)
        if candidate > band_lower and candidate > best:
            best = candidate
    if best <= 0:
        return 0.0
    return math.floor(best * 100) / 100


def describe_fee_schedule(schedule: dict[str, Any] | None = None) -> str:
    """One-line human/LLM-readable rendering of the schedule."""
    sched = schedule if schedule is not None else DEFAULT_FEE_SCHEDULE
    venue = _venue(sched)
    tiers = sched.get("tiers")
    if not tiers:
        parts = [
            f"€{SCALABLE_ORDER_FEE_EUR:,.2f} per order; buys from "
            f"€{SCALABLE_PRIME_MIN_ORDER_EUR:,.0f} in iShares/Vanguard/Xtrackers/Amundi ETFs are free"
        ]
    else:
        parts = []
        lower = 0.0
        for upper, tier_fee in tiers:
            parts.append(f"€{lower:,.0f}–€{float(upper):,.0f}: €{float(tier_fee):,.2f}")
            lower = float(upper)
        parts.append(f"above €{lower:,.0f}: €{float(sched.get('above', 0.0)):,.2f}")
    if venue:
        parts.append(f"plus €{venue:,.2f} venue fee per order")
    return "; ".join(parts)
