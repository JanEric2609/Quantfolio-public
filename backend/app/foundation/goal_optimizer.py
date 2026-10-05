"""Goal-based portfolio optimizer: maps investment goals to target allocations.

Takes a user's goal parameters (risk tolerance, target allocation, time horizon)
and produces recommended portfolio weights using risk-parity or mean-variance.

Includes lifecycle investing (horizon-based allocation), contribution optimization
(LP solver for monthly investment), and goal-aware rebalancing.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RISK_PROFILES = {
    "conservative": {"equity": 30, "bonds": 50, "cash": 15, "alternatives": 5},
    "moderate": {"equity": 55, "bonds": 30, "cash": 10, "alternatives": 5},
    "aggressive": {"equity": 80, "bonds": 10, "cash": 5, "alternatives": 5},
}


def get_goal_allocation(goal_row) -> dict[str, float]:
    """Parse asset class targets from JSON string or fall back to risk tolerance profile.

    Returns a dict mapping asset class names to weights that sum to 1.0.
    """
    allocation: dict[str, float] = {}
    if goal_row.asset_class_targets:
        try:
            raw = json.loads(goal_row.asset_class_targets)
            if isinstance(raw, dict):
                allocation = {k: float(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("Invalid asset_class_targets JSON on goal %s, falling back to risk profile", goal_row.id)

    if not allocation:
        risk = (goal_row.risk_tolerance or "moderate").lower()
        profile = RISK_PROFILES.get(risk, RISK_PROFILES["moderate"])
        allocation = {k: float(v) for k, v in profile.items()}

    total = sum(allocation.values())
    if total > 0 and abs(total - 1.0) > 0.001:
        allocation = {k: v / total for k, v in allocation.items()}

    return allocation


def calculate_savings_trajectory(goal_row) -> dict[str, Any]:
    """Calculate months remaining, required monthly savings, and projected amount.

    Uses a simple compound-growth assumption (5 % annual nominal) to determine
    the monthly contribution needed to reach the target amount by the target date.
    """
    target = goal_row.target_amount or Decimal("0")
    progress = goal_row.progress or Decimal("0")
    monthly = goal_row.monthly_contribution or Decimal("0")

    remaining_amount = target - progress
    months_remaining = 0
    today = date.today()
    if goal_row.target_date and goal_row.target_date > today:
        delta = goal_row.target_date - today
        months_remaining = max(delta.days // 30, 1)

    if months_remaining <= 0 or target <= 0:
        return {
            "months_remaining": months_remaining,
            "required_monthly": Decimal("0"),
            "on_track": remaining_amount <= 0,
            "gap": remaining_amount,
            "projected_amount": progress,
        }

    # Future value of existing progress at market rate, then compute PMT
    # to close the remaining gap: PMT = (target - progress * (1+r)^n) / FV_factor
    monthly_rate = Decimal("0.05") / Decimal("12")
    n = Decimal(str(months_remaining))
    fv_existing = progress * (1 + monthly_rate) ** n
    shortfall = target - fv_existing

    if monthly_rate > 0:
        fv_factor = ((1 + monthly_rate) ** n - 1) / monthly_rate
    else:
        fv_factor = n

    required_monthly = shortfall / fv_factor if fv_factor > 0 and shortfall > 0 else Decimal("0")

    projected = progress
    r = monthly_rate
    for _ in range(months_remaining):
        projected = projected * (1 + r) + monthly

    gap = target - projected
    on_track = gap <= Decimal("0")

    return {
        "months_remaining": months_remaining,
        "required_monthly": required_monthly.quantize(Decimal("0.01")),
        "on_track": on_track,
        "gap": gap.quantize(Decimal("0.01")),
        "projected_amount": projected.quantize(Decimal("0.01")),
    }


def build_goal_portfolio(db: Session, user_id: str, goal_id: str) -> dict[str, Any]:
    """Build a goal-aligned portfolio plan for a given goal.

    Fetches the Goal row, derives target allocation, and optionally runs
    portfolio optimisation constraints.  Falls back to RISK_PROFILES defaults
    on any error.
    """
    from app.foundation.models.entities import Goal

    goal = db.query(Goal).filter(Goal.id == goal_id, Goal.user_id == user_id).first()
    if goal is None:
        return {"error": "Goal not found"}

    try:
        allocation = get_goal_allocation(goal)
    except Exception:
        allocation = {k: v / 100.0 for k, v in RISK_PROFILES.get("moderate", {}).items()}
        logger.exception("Fallback to moderate profile for goal %s", goal_id)

    try:
        trajectory = calculate_savings_trajectory(goal)
    except Exception:
        trajectory = {"months_remaining": 0, "required_monthly": Decimal("0"), "on_track": False, "gap": Decimal("0"), "projected_amount": Decimal("0")}
        logger.exception("Error calculating trajectory for goal %s", goal_id)

    # Try to run mean-variance optimisation on real price data
    optimized_allocation = None
    try:
        from app.foundation.quant_optim import run_optimisation
        from app.foundation.market import history as mkt_history
        from app.foundation.instrument_taxonomy import classify_instrument
        from app.foundation.settings import get_risk_free_rate

        from app.foundation.live_positions import CombinedPosition, combined_positions
        # Synced positions (DKB and Scalable, one row per instrument) carry
        # the real instrument name and a current market value, so both
        # instrument_types and previous_weights (turnover control) can be
        # built directly from the real book — unlike PaperHolding, no
        # separate name-lookup helper is needed here.
        by_ticker: dict[str, CombinedPosition] = {
            h.ticker: h for h in combined_positions(db, user_id) if h.ticker
        }
        tickers = list(by_ticker.keys())
        if len(tickers) >= 2:
            matrix: dict[str, dict[str, float]] = {}
            for ticker in tickers:
                bars = mkt_history(db, ticker, days=730)
                if bars:
                    matrix[ticker] = {}
                    for r in bars:
                        raw_date = r.get("date")
                        if raw_date is None:
                            continue
                        date_key = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date)
                        close_val = r.get("close")
                        if close_val is not None:
                            matrix[ticker][date_key] = float(close_val)
            if len(matrix) >= 2:
                instrument_types = {
                    t: classify_instrument(t, name=by_ticker[t].name) for t in matrix
                }
                total_value = sum(float(by_ticker[t].value) for t in matrix)
                previous_weights = (
                    {t: float(by_ticker[t].value) / total_value for t in matrix}
                    if total_value > 0
                    else None
                )
                result = run_optimisation(
                    matrix,
                    objective="min_risk",
                    risk_free_rate=get_risk_free_rate(db),
                    instrument_types=instrument_types,
                    previous_weights=previous_weights,
                    max_turnover=0.20,
                )
                if result and result.get("weights"):
                    optimized_allocation = result["weights"]
    except Exception:
        logger.debug("Mean-variance optimisation unavailable for goal %s, using static profile", goal_id)

    return {
        "allocation": {k: round(v * 100, 2) for k, v in allocation.items()},
        "optimized_allocation": optimized_allocation,
        "trajectory": trajectory,
        "goal": {
            "id": goal.id,
            "title": goal.title,
            "target_amount": str(goal.target_amount) if goal.target_amount else None,
            "target_date": goal.target_date.isoformat() if goal.target_date else None,
            "risk_tolerance": goal.risk_tolerance,
            "monthly_contribution": str(goal.monthly_contribution) if goal.monthly_contribution else None,
            "progress": str(goal.progress) if goal.progress else None,
        },
    }


# ---------------------------------------------------------------------------
# Lifecycle investing — horizon-based equity allocation
# ---------------------------------------------------------------------------

# Maps years-to-horizon → equity/bond allocation using a glide path.
# Inspired by target-date fund glide paths (Vanguard, BlackRock).
LIFECYCLE_GLIDE_PATH: list[tuple[int, float, float]] = [
    # (horizon_years, equity_pct, bond_pct)  — cash+alternatives fill remainder
    (1, 20, 50),   # <2yr: capital preservation
    (2, 30, 45),   # 2-3yr: very conservative
    (3, 40, 40),   # 3-4yr: conservative
    (5, 55, 30),   # 5-6yr: moderate-conservative
    (7, 65, 25),   # 7-8yr: moderate
    (10, 70, 20),  # 9-10yr: moderate-aggressive
    (15, 80, 15),  # 11-15yr: aggressive
    (20, 85, 10),  # 16-20yr: very aggressive
    (30, 90, 5),   # 20yr+: maximum equity
]


def lifecycle_investing(horizon_years: float) -> dict[str, float]:
    """Return asset class allocation based on investment time horizon.

    Uses a glide path that shifts from equity-heavy (long horizon) to
    bond/cash-heavy (short horizon). Cash fills the remainder after
    equity + bonds (typically 0-15%).

    Args:
        horizon_years: Time horizon in years (e.g. 5.0, 10.0, 20.0).

    Returns:
        Dict mapping asset class names to weights summing to 1.0.
    """
    h = max(0.0, float(horizon_years))

    # Find the two bracketing points on the glide path
    prev_h, prev_eq, prev_bd = 0, 20, 50
    for point_h, eq, bd in LIFECYCLE_GLIDE_PATH:
        if h <= point_h:
            # Linear interpolation between prev and current
            t = (h - prev_h) / max(point_h - prev_h, 1)
            equity = (prev_eq + t * (eq - prev_eq)) / 100.0
            bonds = (prev_bd + t * (bd - prev_bd)) / 100.0
            cash = max(0.0, 1.0 - equity - bonds - 0.05)  # leave room for alternatives
            alternatives = max(0.0, 1.0 - equity - bonds - cash)
            return {
                "equity": round(equity, 4),
                "bonds": round(bonds, 4),
                "cash": round(cash, 4),
                "alternatives": round(alternatives, 4),
            }
        prev_h, prev_eq, prev_bd = point_h, eq, bd

    # Beyond longest horizon
    return {"equity": 0.90, "bonds": 0.05, "cash": 0.02, "alternatives": 0.03}


# ---------------------------------------------------------------------------
# Contribution optimizer — LP solver for monthly investment allocation
# ---------------------------------------------------------------------------

def contribution_optimizer(
    db: Session,
    user_id: str,
    monthly_income: float | None = None,
    monthly_expenses: float | None = None,
    emergency_fund_months: float = 6.0,
    risk_free_rate_annual: float = 0.03,
) -> dict[str, Any]:
    """Optimize how to split monthly investment across goals.

    Uses scipy.optimize.linprog to solve:
      maximize: sum of (goal_priority_weight * allocation_fraction)
      subject to: sum of allocations <= available_savings,
                  each allocation >= 0,
                  short-term goals get priority (higher weight).

    Args:
        db: Database session.
        user_id: Current user ID.
        monthly_income: User's monthly net income (if known).
        monthly_expenses: User's monthly fixed expenses (if known).
        emergency_fund_months: How many months of expenses to keep as emergency.
        risk_free_rate_annual: Annual risk-free rate for present value calculations.

    Returns:
        Dict with recommended_amount, per_goal splits, total_goals, and diagnostics.
    """
    from app.foundation.models.entities import Goal

    goals = db.query(Goal).filter(Goal.user_id == user_id).all()
    if not goals:
        return {
            "recommended_monthly": 0,
            "per_goal": [],
            "total_goals": 0,
            "diagnostics": {"reason": "No goals defined"},
        }

    # Build per-goal parameters
    goal_params: list[dict[str, Any]] = []
    for g in goals:
        target = float(g.target_amount or 0)
        progress = float(g.progress or 0)
        monthly = float(g.monthly_contribution or 0)
        remaining = max(0.0, target - progress)

        months_left = 0
        today = date.today()
        if g.target_date and g.target_date > today:
            months_left = max((g.target_date - today).days // 30, 1)

        # Priority weight: sooner deadlines and larger gaps get higher weight
        priority = 1.0
        if months_left > 0:
            priority = 10.0 / months_left  # sooner = higher priority
        elif remaining > 0:
            priority = 2.0  # overdue goals get high priority

        # Risk-based allocation
        allocation = get_goal_allocation(g)

        goal_params.append({
            "goal_id": g.id,
            "title": g.title,
            "target": target,
            "progress": progress,
            "remaining": remaining,
            "months_left": months_left,
            "existing_monthly": monthly,
            "priority": priority,
            "allocation": allocation,
            "risk_tolerance": g.risk_tolerance or "moderate",
        })

    # Estimate available savings
    available_savings = 0.0
    if monthly_income and monthly_expenses:
        available_savings = monthly_income - monthly_expenses
    elif monthly_income:
        # Rough estimate: save 20-30% of income
        available_savings = monthly_income * 0.25

    # If no income data, sum existing monthly contributions as baseline
    if available_savings <= 0:
        available_savings = sum(p["existing_monthly"] for p in goal_params)
        if available_savings <= 0:
            available_savings = 200.0  # default suggestion

    # Emergency fund: reserve first
    emergency_reserve = 0.0
    if monthly_expenses:
        emergency_reserve = monthly_expenses * emergency_fund_months

    # LP: maximize priority-weighted allocation
    n = len(goal_params)
    lp_success = False
    try:
        from scipy.optimize import linprog

        # Minimize negative of priority-weighted allocation
        c = [-p["priority"] for p in goal_params]

        # Constraint: sum of allocations <= available_savings
        A_ub = [[1.0] * n]
        b_ub = [available_savings]

        # Bounds: each allocation >= 0, capped at available_savings to avoid degeneracy
        bounds = [(0, available_savings)] * n

        result = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
        if result.success:
            allocations = [round(float(x), 2) for x in result.x]
            lp_success = True
        else:
            # Fallback: proportional allocation by priority
            total_priority = sum(p["priority"] for p in goal_params)
            allocations = [
                round(p["priority"] / total_priority * available_savings, 2)
                for p in goal_params
            ]
    except Exception:
        logger.debug("LP solver unavailable, using priority-proportional allocation")
        total_priority = sum(p["priority"] for p in goal_params)
        allocations = [
            round(p["priority"] / total_priority * available_savings, 2)
            for p in goal_params
        ]

    per_goal = []
    for i, p in enumerate(goal_params):
        alloc = allocations[i]
        # Blend lifecycle allocation with goal-specific allocation
        horizon_years = p["months_left"] / 12.0 if p["months_left"] > 0 else 5.0
        lifecycle = lifecycle_investing(horizon_years)
        # Weight: 60% goal-specific, 40% lifecycle
        blended = {}
        all_classes = set(list(p["allocation"].keys()) + list(lifecycle.keys()))
        for cls in all_classes:
            g_val = p["allocation"].get(cls, 0)
            l_val = lifecycle.get(cls, 0)
            blended[cls] = round(g_val * 0.6 + l_val * 0.4, 4)

        per_goal.append({
            "goal_id": p["goal_id"],
            "title": p["title"],
            "recommended_monthly": alloc,
            "existing_monthly": p["existing_monthly"],
            "gap": round(alloc - p["existing_monthly"], 2),
            "remaining": round(p["remaining"], 2),
            "months_left": p["months_left"],
            "allocation": blended,
            "risk_tolerance": p["risk_tolerance"],
        })

    return {
        "recommended_monthly": round(sum(allocations), 2),
        "per_goal": per_goal,
        "total_goals": n,
        "available_savings": round(available_savings, 2),
        "emergency_reserve": round(emergency_reserve, 2),
        "diagnostics": {
            "method": "linprog" if lp_success else "priority-proportional" if n > 0 else "none",
            "emergency_fund_months": emergency_fund_months,
        },
    }


# ---------------------------------------------------------------------------
# Goal-aware rebalancing
# ---------------------------------------------------------------------------

def rebalance_with_goals(db: Session, user_id: str) -> dict[str, Any]:
    """Generate rebalancing suggestions constrained by goal timelines.

    Short-term goals (<2yr) → cash / short-duration bonds
    Medium-term (2-7yr) → balanced (equity/bond mix)
    Long-term (>7yr) → growth (equity-heavy)

    Uses real DKB holdings if available, otherwise falls back to goal allocations.
    """
    from app.foundation.models.entities import Goal

    goals = db.query(Goal).filter(Goal.user_id == user_id).all()
    if not goals:
        return {
            "available": False,
            "reason": "No goals defined",
            "suggestions": [],
        }

    # Classify goals by horizon
    short_term: list[dict] = []   # <2yr
    medium_term: list[dict] = []  # 2-7yr
    long_term: list[dict] = []    # >7yr

    today = date.today()
    for g in goals:
        months_left = 0
        if g.target_date and g.target_date > today:
            months_left = max((g.target_date - today).days // 30, 1)
        years_left = months_left / 12.0

        entry = {
            "goal_id": g.id,
            "title": g.title,
            "years_left": round(years_left, 1),
            "risk_tolerance": g.risk_tolerance or "moderate",
            "allocation": get_goal_allocation(g),
        }

        if years_left < 2:
            short_term.append(entry)
        elif years_left <= 7:
            medium_term.append(entry)
        else:
            long_term.append(entry)

    # Build target allocation based on goal buckets
    target_allocation: dict[str, float] = {}
    n_goals = len(goals)
    if n_goals == 0:
        return {"available": False, "reason": "No goals", "suggestions": []}

    # Weight each bucket by number of goals
    bucket_weights = {
        "short": len(short_term) / n_goals if short_term else 0,
        "medium": len(medium_term) / n_goals if medium_term else 0,
        "long": len(long_term) / n_goals if long_term else 0,
    }

    # Short-term: 20% equity, 40% bonds, 30% cash, 10% alternatives
    short_alloc = {"equity": 0.20, "bonds": 0.40, "cash": 0.30, "alternatives": 0.10}
    # Medium-term: 55% equity, 30% bonds, 10% cash, 5% alternatives
    medium_alloc = {"equity": 0.55, "bonds": 0.30, "cash": 0.10, "alternatives": 0.05}
    # Long-term: 85% equity, 10% bonds, 3% cash, 2% alternatives
    long_alloc = {"equity": 0.85, "bonds": 0.10, "cash": 0.03, "alternatives": 0.02}

    all_classes = {"equity", "bonds", "cash", "alternatives"}
    for cls in all_classes:
        target_allocation[cls] = (
            short_alloc[cls] * bucket_weights["short"]
            + medium_alloc[cls] * bucket_weights["medium"]
            + long_alloc[cls] * bucket_weights["long"]
        )

    # Normalize
    total = sum(target_allocation.values())
    if total > 0:
        target_allocation = {k: round(v / total, 4) for k, v in target_allocation.items()}

    # Try to get current real allocation
    current_allocation: dict[str, float] | None = None
    try:
        from app.foundation.portfolio.bridge import get_real_holdings_summary
        summary = get_real_holdings_summary(db, user_id)
        if summary.get("total_value", 0) > 0:
            # Map tickers to asset classes (simple heuristic)
            by_ticker = summary.get("by_ticker", {})
            if by_ticker:
                total_val = summary["total_value"]
                # Default: treat all as equity (can be refined with asset data)
                equity_pct = sum(t.get("current_value", 0) for t in by_ticker.values()) / total_val
                current_allocation = {
                    "equity": round(equity_pct, 4),
                    "bonds": 0.0,
                    "cash": max(0, 1.0 - equity_pct),
                    "alternatives": 0.0,
                }
    except Exception:
        logger.debug("Could not fetch real holdings for goal rebalance")

    # Generate suggestions
    suggestions = []
    if current_allocation:
        for cls in all_classes:
            current = current_allocation.get(cls, 0)
            target = target_allocation.get(cls, 0)
            diff = target - current
            if abs(diff) > 0.02:  # >2% drift threshold
                suggestions.append({
                    "asset_class": cls,
                    "current": round(current * 100, 1),
                    "target": round(target * 100, 1),
                    "action": "increase" if diff > 0 else "decrease",
                    "drift_pct": round(abs(diff) * 100, 1),
                })

    return {
        "available": current_allocation is not None,
        "current_allocation": {k: round(v * 100, 1) for k, v in (current_allocation or {}).items()},
        "target_allocation": {k: round(v * 100, 1) for k, v in target_allocation.items()},
        "bucket_weights": {k: round(v * 100, 1) for k, v in bucket_weights.items()},
        "goal_buckets": {
            "short_term": [{"title": g["title"], "years_left": g["years_left"]} for g in short_term],
            "medium_term": [{"title": g["title"], "years_left": g["years_left"]} for g in medium_term],
            "long_term": [{"title": g["title"], "years_left": g["years_left"]} for g in long_term],
        },
        "suggestions": suggestions,
        "n_goals": n_goals,
    }
