"""Long-history evidence for the plan's asset-mix rules (ADR 0022, plan 2026-10-14 WP-C).

Reads the JST Macrohistory panel from a local file (CC BY-NC-SA: never in the
repository or the public export) and simulates a monthly saver's plan under
each pre-registered rule with a stationary block bootstrap by country.
``plan_fan`` reuses that construction for the plan's own fan and one-year band (D6).
"""
from app.lab.long_history.jst_loader import ASSETS, COUNTRIES, home_panel, load_jst
from app.lab.long_history.plan_bootstrap import (
    RULES,
    PlanCosts,
    bootstrap_indices,
    certainty_equivalent,
    simulate_rule,
)
from app.lab.long_history.plan_fan import Mix, jst_path, load_histories, one_year_band
from app.lab.long_history.plan_fan import project as project_plan

__all__ = [
    "ASSETS",
    "COUNTRIES",
    "Mix",
    "RULES",
    "PlanCosts",
    "bootstrap_indices",
    "certainty_equivalent",
    "home_panel",
    "jst_path",
    "load_histories",
    "load_jst",
    "one_year_band",
    "project_plan",
    "simulate_rule",
]
