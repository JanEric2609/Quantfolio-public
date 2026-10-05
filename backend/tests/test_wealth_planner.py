"""Monte Carlo planner: log-space drift, median first, unit-variance fat tails."""
import math

import numpy as np
import pytest

from app.foundation.wealth_planner import PlanInputs, _shocks, project


def test_without_volatility_it_is_the_compound_annuity():
    p = PlanInputs(start_value=10_000, monthly_contribution=100, years=10, real_return=0.042,
                   volatility=0.0, paths=50, calibration_years=0)
    out = project(p)
    g = (1.042) ** (1 / 12) - 1
    fv = 10_000 * 1.042 ** 10 + 100 * sum((1 + g) ** k for k in range(1, 121))
    assert out["terminal"]["median"] == pytest.approx(fv, rel=1e-9)
    assert out["fan"][-1]["contributed"] == 10_000 + 100 * 120


def test_the_median_grows_at_the_compound_rate_and_the_mean_above_it():
    """30 years at 7 % geometric, 16 % vol: median x 7.6, mean x 7.6 * exp(0.16^2 / 2 * 30)."""
    p = PlanInputs(start_value=1.0, monthly_contribution=0.0, years=30, real_return=0.07,
                   volatility=0.16, nu=0, calibration_years=0, paths=20_000, seed=1)
    out = project(p)
    assert out["terminal"]["median"] == pytest.approx(1.07 ** 30, rel=0.03)
    assert out["terminal"]["mean"] == pytest.approx(1.07 ** 30 * math.exp(0.16 ** 2 / 2 * 30), rel=0.06)


def test_student_t_shocks_have_unit_variance():
    z = _shocks(np.random.default_rng(0), (400_000, 1), 5.0)
    assert float(np.var(z)) == pytest.approx(1.0, abs=0.03)


def test_drift_uncertainty_widens_the_fan():
    base = dict(start_value=10_000, monthly_contribution=0, years=30, real_return=0.042, volatility=0.16,
                nu=0, paths=20_000, seed=3)
    fixed = project(PlanInputs(**base, calibration_years=0))["fan"][-1]
    uncertain = project(PlanInputs(**base, calibration_years=25))["fan"][-1]
    assert (uncertain["p95"] - uncertain["p5"]) > (fixed["p95"] - fixed["p5"])


def test_goal_probability_with_its_standard_error_and_drift_sensitivity():
    out = project(PlanInputs(start_value=30_000, monthly_contribution=500, years=20, real_return=0.042,
                             volatility=0.16, goal=250_000, paths=10_000))
    g = out["goal"]
    assert 0 < g["probability"] < 1
    assert g["mc_standard_error"] == pytest.approx(math.sqrt(g["probability"] * (1 - g["probability"]) / 10_000))
    assert g["probability_low_drift"] < g["probability"] < g["probability_high_drift"]
