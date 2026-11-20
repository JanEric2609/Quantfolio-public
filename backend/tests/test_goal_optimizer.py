"""Coverage for goal_optimizer pure calculators + no-goals path (issue #136).

goal_optimizer.py had zero test references. These characterization tests pin
the current behaviour of the pure functions (allocation parsing, savings
trajectory, lifecycle glide path) and the empty-goals branch of the LP
contribution optimizer.
"""
import json
from datetime import date, timedelta
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models import entities as _entities  # noqa: F401  (register tables for create_all)
from app.foundation.goal_optimizer import (
    calculate_savings_trajectory,
    contribution_optimizer,
    get_goal_allocation,
    lifecycle_investing,
)


class _Goal:
    """Duck-typed stand-in for the Goal ORM row used by the pure functions."""

    def __init__(self, **kw):
        self.id = kw.get("id", "g1")
        self.asset_class_targets = kw.get("asset_class_targets")
        self.risk_tolerance = kw.get("risk_tolerance")
        self.target_amount = kw.get("target_amount")
        self.progress = kw.get("progress")
        self.monthly_contribution = kw.get("monthly_contribution")
        self.target_date = kw.get("target_date")


# --- get_goal_allocation ---------------------------------------------------

def test_allocation_from_explicit_targets_already_normalized():
    goal = _Goal(asset_class_targets=json.dumps({"equity": 0.6, "bonds": 0.4}))
    alloc = get_goal_allocation(goal)
    assert alloc == {"equity": 0.6, "bonds": 0.4}


def test_allocation_normalizes_when_targets_do_not_sum_to_one():
    goal = _Goal(asset_class_targets=json.dumps({"equity": 60, "bonds": 40}))
    alloc = get_goal_allocation(goal)
    assert alloc["equity"] == 0.6
    assert alloc["bonds"] == 0.4
    assert abs(sum(alloc.values()) - 1.0) < 1e-9


def test_allocation_falls_back_to_risk_profile_when_no_targets():
    goal = _Goal(asset_class_targets=None, risk_tolerance="aggressive")
    alloc = get_goal_allocation(goal)
    # aggressive profile 80/10/5/5 normalized.
    assert alloc == {"equity": 0.8, "bonds": 0.1, "cash": 0.05, "alternatives": 0.05}


def test_allocation_invalid_json_falls_back_to_moderate():
    goal = _Goal(asset_class_targets="{not valid json", risk_tolerance=None)
    alloc = get_goal_allocation(goal)
    assert alloc == {"equity": 0.55, "bonds": 0.30, "cash": 0.10, "alternatives": 0.05}


# --- calculate_savings_trajectory ------------------------------------------

def test_trajectory_no_target_date_reports_gap_only():
    goal = _Goal(
        target_amount=Decimal("10000"),
        progress=Decimal("4000"),
        monthly_contribution=Decimal("100"),
        target_date=None,
    )
    result = calculate_savings_trajectory(goal)
    assert result["months_remaining"] == 0
    assert result["required_monthly"] == Decimal("0")
    assert result["on_track"] is False  # 6000 gap remaining
    assert result["gap"] == Decimal("6000")


def test_trajectory_already_met_is_on_track():
    goal = _Goal(
        target_amount=Decimal("5000"),
        progress=Decimal("5000"),
        monthly_contribution=Decimal("0"),
        target_date=None,
    )
    result = calculate_savings_trajectory(goal)
    assert result["on_track"] is True


def test_trajectory_future_date_requires_positive_monthly():
    goal = _Goal(
        target_amount=Decimal("12000"),
        progress=Decimal("0"),
        monthly_contribution=Decimal("0"),
        target_date=date.today() + timedelta(days=365),
    )
    result = calculate_savings_trajectory(goal)
    assert result["months_remaining"] >= 11
    assert result["required_monthly"] > Decimal("0")


# --- lifecycle_investing ---------------------------------------------------

def test_lifecycle_weights_sum_to_one_across_horizons():
    for h in (0, 1, 4, 7, 15, 100):
        alloc = lifecycle_investing(h)
        assert abs(sum(alloc.values()) - 1.0) < 1e-6, h


def test_lifecycle_equity_rises_with_horizon():
    short = lifecycle_investing(1)["equity"]
    mid = lifecycle_investing(7)["equity"]
    long = lifecycle_investing(30)["equity"]
    assert short < mid < long


def test_lifecycle_interpolates_between_points():
    # h=4 sits halfway between the (3, 40, 40) and (5, 55, 30) glide points.
    alloc = lifecycle_investing(4)
    assert alloc["equity"] == 0.475
    assert alloc["bonds"] == 0.35


def test_lifecycle_beyond_longest_horizon_is_max_equity():
    assert lifecycle_investing(50) == {"equity": 0.90, "bonds": 0.05, "cash": 0.02, "alternatives": 0.03}


# --- contribution_optimizer (DB-backed, empty path) ------------------------

def test_contribution_optimizer_no_goals_returns_empty_diagnostics():
    db = _memory_db()
    result = contribution_optimizer(db, "user-with-no-goals")
    assert result["total_goals"] == 0
    assert result["per_goal"] == []
    assert result["diagnostics"]["reason"] == "No goals defined"
