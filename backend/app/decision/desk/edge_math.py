"""Cost and sample-size arithmetic for the index CFD card and the Desk page (ADR 0027 amendment 2026-11-25).

Pure functions, display only: nothing here changes a rule, a limit, a position or the pre-registered pass test.
"""
from __future__ import annotations

import math
from decimal import Decimal
from statistics import NormalDist

_N = NormalDist()


def break_even_hit_rate(cost: Decimal | float, move: Decimal | float) -> float | None:
    """Hit rate at which a trade that wins or loses ``move`` and pays ``cost`` per round trip breaks even:
    ``0.5 + cost / (2 * move)`` (cost and move in the same unit, points or bp). ``None`` without a move."""
    m, c = float(move), float(cost)
    if m <= 0 or c < 0:
        return None
    return min(1.0, 0.5 + c / (2 * m))


def trades_needed(hit_rate: float, *, null: float = 0.5, alpha: float = 0.05, power: float = 0.8) -> int | None:
    """Trades to tell a *hit_rate* from the *null* hit rate (one-sided, equal win and loss size). The default null is a
    fair coin; the card passes its own break-even rate. ``n = ((z_a sqrt(p0 (1-p0)) + z_b sqrt(p (1-p))) / (p - p0)) ** 2``;
    against a coin 60 % needs about 153, 55 % about 617."""
    if not 0 < null < hit_rate < 1:
        return None
    za, zb = _N.inv_cdf(1 - alpha), _N.inv_cdf(power)
    num = za * math.sqrt(null * (1 - null)) + zb * math.sqrt(hit_rate * (1 - hit_rate))
    return math.ceil((num / (hit_rate - null)) ** 2)


def pair_risk_multiple(rho: float, n: int = 2) -> float:
    """Risk of *n* equal-risk legs with pairwise correlation *rho*, in units of one leg: ``sqrt(n + n(n-1) rho)``.
    Two DAX/ES50 legs at rho 0.9 carry about 1.95 R, not 2 R and not sqrt(2)."""
    return math.sqrt(max(0.0, n + n * (n - 1) * rho))


def cost_line(spread_points: Decimal | float, typical_move_points: Decimal | float) -> str | None:
    """The card's one-line cost check, or ``None`` when the typical move is unknown."""
    be = break_even_hit_rate(spread_points, typical_move_points)
    if be is None:
        return None
    share = float(spread_points) / float(typical_move_points)
    need = trades_needed(min(be + 0.05, 0.99), null=be)
    tail = f"; showing a hit rate {be + 0.05:.0%} needs about {need} trades to tell it from break-even" if need else ""
    return (f"Cost check: the spread is {share:.1%} of the typical move, so the trade breaks even at a "
            f"{be:.1%} hit rate{tail}.")
