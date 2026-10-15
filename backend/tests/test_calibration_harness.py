"""The calibration harness (plan 2026-10-14, WP-D2)."""
from __future__ import annotations

import numpy as np
import pytest

from app.foundation import forecast_verification as fv
from app.lab.calibration import MODELS, TestSpec, crossing_days, e_paths, simulate, time_to_verdict, type_i_error

F1_LIKE = TestSpec("F1 skill", clip=0.05, prior_var=0.0121, cap_factor=0.75, threshold=40.0, direction="skill")


def test_the_vectorised_bettor_is_the_production_bettor():
    rng = np.random.default_rng(0)
    y = rng.random((4, 300))
    fast = e_paths(y, 0.0121, 0.75)
    for i in range(4):
        slow = fv.e_process_bernoulli(y[i], 0.5, 0.0121, cap_factor=0.75).path
        np.testing.assert_allclose(fast[i], slow, rtol=1e-12)


def test_harm_and_futility_bet_on_the_other_side_of_the_margin():
    x = np.array([[0.0, 0.001, -0.002]])
    futility = TestSpec("U", clip=0.0225, prior_var=0.0121, cap_factor=0.75, threshold=20.0,
                        direction="futility", shift=0.0003)
    y = futility.to_unit(x)
    expected = 1.0 - (np.clip(x - 0.0003, -0.0225, 0.0225) + 0.0225) / 0.045
    np.testing.assert_allclose(y, expected)


@pytest.mark.parametrize("model", sorted(MODELS))
def test_every_model_has_the_stated_mean_and_noise(model):
    x = simulate(model, 200, 2000, 0.0002, 0.005, seed=3)
    assert x.shape == (200, 2000)
    assert abs(x.mean() - 0.0002) < 0.0002
    assert x.std() == pytest.approx(0.005, rel=0.15)


def test_false_alarms_stay_below_five_percent_over_twenty_years():
    rate = type_i_error(F1_LIKE, "iid_normal", 0.011, years=20, n_paths=300, seed=4)
    assert rate <= 0.05


def test_a_large_edge_is_found_and_crossing_days_are_one_based():
    x = simulate("iid_normal", 50, 2000, 0.02 / 21, 0.005, seed=5)
    days = crossing_days(x, F1_LIKE)
    assert (days > 0).all() and days.min() >= 1
    t = time_to_verdict(F1_LIKE, "iid_normal", 0.02, 0.005, years=10, n_paths=100, seed=6)
    assert t["q50_years"] is not None and t["q50_years"] < 3 and t["p_within_10y"] == 1.0


def test_overlap_paths_are_not_recentred_on_their_own_mean():
    from app.lab.calibration import simulate

    paths = simulate("overlap_21d", 400, 252, 0.0, 0.01, seed=3)
    means = paths.mean(axis=1)
    # Re-centring would make every path's mean exactly 0; persistent drifts keep them spread.
    assert means.std() > 1e-4
    assert abs(paths.std() - 0.01) < 0.001


def test_noise_check_fails_few_clean_series_and_most_persistent_ones():
    from app.decision.verification.daily_tests import NOISE_CHECK_LOOKS
    from app.lab.calibration import noise_check_fail_rates

    clean = noise_check_fail_rates("iid_normal", NOISE_CHECK_LOOKS, n_paths=2000, seed=3)
    stressed = noise_check_fail_rates("overlap_21d", NOISE_CHECK_LOOKS, n_paths=500, seed=3)
    assert clean["any_look"] < 0.08 and clean["first_look"] <= clean["any_look"]
    assert stressed["any_look"] > 0.95
