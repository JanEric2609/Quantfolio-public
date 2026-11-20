"""Position size and stop from the per-trade loss budget (ADR 0027)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .limits import LIMITS


@dataclass(frozen=True)
class Sizing:
    units: int
    stop_price: Decimal
    stop_fraction: Decimal
    notional: Decimal
    worst_case_loss: Decimal
    reason: str | None = None  # "too_small" when no whole unit fits the budget


def stop_fraction(leverage_or_omega: Decimal, sigma: Decimal) -> Decimal:
    """An adverse underlying move of one daily sigma, through the product's leverage, clamped to 3-30 %."""
    raw = abs(leverage_or_omega) * sigma * LIMITS.stop_sigmas
    return min(max(raw, LIMITS.stop_min_fraction), LIMITS.stop_max_fraction)


def size_position(ask: Decimal, leverage_or_omega: Decimal, sigma: Decimal, pot_left: Decimal) -> Sizing:
    fraction = stop_fraction(leverage_or_omega, sigma)
    stop = (ask * (1 - fraction)).quantize(Decimal("0.0001"))
    budget = LIMITS.max_loss_per_trade - 2 * LIMITS.fee_per_order
    per_unit_risk = ask - stop
    if per_unit_risk <= 0 or budget <= 0:
        return Sizing(0, stop, fraction, Decimal(0), Decimal(0), "too_small")
    units = math.floor(budget / per_unit_risk)
    units = min(units, math.floor(pot_left / ask)) if ask > 0 else 0
    if units < 1:
        return Sizing(0, stop, fraction, Decimal(0), Decimal(0), "too_small")
    return Sizing(units, stop, fraction, ask * units, per_unit_risk * units + 2 * LIMITS.fee_per_order)
