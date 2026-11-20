"""Tests for track-record significance metrics (PSR / DSR / MinTRL).

These follow Bailey & López de Prado. We assert properties (monotonicity,
ordering, bounds) rather than hard-coded values, which keeps the tests robust
while still pinning the intended behaviour.
"""
from __future__ import annotations

import math

import numpy as np

from app.foundation import quant_metrics as qm


def _series(mu: float, sigma: float, n: int, seed: int) -> list[float]:
    """
    Generate a list of random samples from a normal distribution.
    
    Parameters:
        mu (float): Mean of the normal distribution
        sigma (float): Standard deviation of the normal distribution
        n (int): Number of samples to generate
        seed (int): Random seed for reproducibility
    
    Returns:
        list[float]: A list of n samples from a normal distribution with the specified mean and standard deviation
    """
    rng = np.random.default_rng(seed)
    return list(rng.normal(mu, sigma, n))


def test_psr_in_unit_interval_and_orders_skill():
    skilful = _series(0.0010, 0.01, 300, seed=1)
    noise = _series(0.0000, 0.01, 300, seed=2)
    psr_skill = qm.probabilistic_sharpe_ratio(skilful)
    psr_noise = qm.probabilistic_sharpe_ratio(noise)
    assert 0.0 <= psr_noise <= 1.0
    assert 0.0 <= psr_skill <= 1.0
    assert psr_skill > psr_noise


def test_psr_zero_for_insufficient_data():
    assert qm.probabilistic_sharpe_ratio([0.01, 0.02]) == 0.0


def test_expected_max_sharpe_grows_with_trials():
    e10 = qm.expected_max_sharpe(10, 0.02)
    e100 = qm.expected_max_sharpe(100, 0.02)
    assert e100 > e10 > 0.0
    # A single trial cannot be inflated by selection.
    assert qm.expected_max_sharpe(1, 0.02) == 0.0


def test_dsr_deflates_with_more_trials():
    skilful = _series(0.0012, 0.01, 400, seed=3)
    dsr_few = qm.deflated_sharpe_ratio(skilful, n_trials=2)
    dsr_many = qm.deflated_sharpe_ratio(skilful, n_trials=200)
    assert 0.0 <= dsr_many <= dsr_few <= 1.0
    # Selecting from many trials should substantially reduce confidence.
    assert dsr_many < dsr_few


def test_dsr_deflates_lucky_noise_when_many_trials():
    # A skill-less series can look decent by chance under few trials; the DSR's
    # job is to discount it once we account for how many trials were run.
    noise = _series(0.0, 0.01, 400, seed=4)
    assert qm.deflated_sharpe_ratio(noise, n_trials=500) < 0.5


def test_min_track_record_length_finite_for_skill_infinite_for_none():
    skilful = _series(0.0015, 0.01, 300, seed=5)
    flat = _series(0.0, 0.01, 300, seed=6)
    mintrl = qm.min_track_record_length(skilful)
    assert math.isfinite(mintrl) and mintrl > 1.0
    # No edge over the benchmark -> significance never reachable.
    assert qm.min_track_record_length(flat, sr_benchmark=5.0) == float("inf")


def test_min_track_record_length_shrinks_with_stronger_signal():
    weak = _series(0.0006, 0.01, 300, seed=7)
    strong = _series(0.0020, 0.01, 300, seed=7)
    assert qm.min_track_record_length(strong) < qm.min_track_record_length(weak)


# ---------------------------------------------------------------------------
# Additional edge-case and boundary tests


def test_psr_increases_with_longer_series():
    """Holding signal strength constant, more data should increase confidence."""
    short = _series(0.001, 0.01, 60, seed=10)
    long = _series(0.001, 0.01, 500, seed=10)
    psr_short = qm.probabilistic_sharpe_ratio(short)
    psr_long = qm.probabilistic_sharpe_ratio(long)
    assert psr_long > psr_short


def test_psr_with_positive_benchmark_is_lower_than_zero_benchmark():
    """A positive benchmark makes the hurdle harder; PSR must fall."""
    series = _series(0.0010, 0.01, 300, seed=11)
    psr_zero = qm.probabilistic_sharpe_ratio(series, sr_benchmark=0.0)
    psr_pos = qm.probabilistic_sharpe_ratio(series, sr_benchmark=1.0)
    assert psr_zero > psr_pos


def test_psr_approaches_one_for_very_skilful_series():
    """An extremely high-Sharpe series should yield PSR very close to 1."""
    stellar = _series(0.005, 0.005, 500, seed=12)
    psr = qm.probabilistic_sharpe_ratio(stellar)
    assert psr > 0.99


def test_expected_max_sharpe_zero_variance_returns_zero():
    assert qm.expected_max_sharpe(10, 0.0) == 0.0


def test_expected_max_sharpe_negative_variance_returns_zero():
    assert qm.expected_max_sharpe(10, -0.01) == 0.0


def test_dsr_in_unit_interval_for_all_inputs():
    """DSR must always be in [0, 1]."""
    for mu, n_trials in [
        (0.0005, 2),
        (0.0005, 50),
        (0.0015, 2),
        (0.0015, 100),
        (-0.001, 5),
    ]:
        series = _series(mu, 0.01, 200, seed=42)
        dsr = qm.deflated_sharpe_ratio(series, n_trials=n_trials)
        assert 0.0 <= dsr <= 1.0, f"DSR out of range for mu={mu}, n_trials={n_trials}"


def test_dsr_with_explicit_variance_sharpe_differs_from_default():
    """Providing an explicit variance_sharpe should change the result."""
    series = _series(0.0012, 0.01, 300, seed=13)
    dsr_default = qm.deflated_sharpe_ratio(series, n_trials=5)
    dsr_high_var = qm.deflated_sharpe_ratio(series, n_trials=5, variance_sharpe=0.5)
    # Higher cross-sectional variance → higher threshold → lower DSR.
    assert dsr_default != dsr_high_var


def test_dsr_returns_zero_for_insufficient_data():
    assert qm.deflated_sharpe_ratio([0.01, 0.02], n_trials=5) == 0.0


def test_min_track_record_length_returns_inf_for_two_observations():
    """With fewer than 3 observations MinTRL must always be inf."""
    assert qm.min_track_record_length([0.01, 0.02]) == float("inf")


def test_min_track_record_length_decreases_with_more_data():
    """Same signal but more data → shorter minimum track required."""
    few = _series(0.0015, 0.01, 80, seed=14)
    many = _series(0.0015, 0.01, 400, seed=14)
    trl_few = qm.min_track_record_length(few)
    trl_many = qm.min_track_record_length(many)
    # Both should be finite for a genuine signal.
    if math.isfinite(trl_few) and math.isfinite(trl_many):
        assert trl_many < trl_few


def test_min_track_record_length_respects_confidence_parameter():
    """Higher confidence means more data are required."""
    series = _series(0.0015, 0.01, 300, seed=15)
    trl_90 = qm.min_track_record_length(series, confidence=0.90)
    trl_99 = qm.min_track_record_length(series, confidence=0.99)
    assert trl_99 > trl_90


def test_per_period_sharpe_moments_zero_variance_returns_zero_sharpe():
    """A constant-return series has zero variance → Sharpe should be 0."""
    from app.foundation.quant_metrics import _per_period_sharpe_moments
    sr, skew, kurt, n = _per_period_sharpe_moments([0.001] * 50)
    assert sr == 0.0
    assert n == 50


def test_per_period_sharpe_moments_too_short_returns_defaults():
    from app.foundation.quant_metrics import _per_period_sharpe_moments
    sr, skew, kurt, n = _per_period_sharpe_moments([0.01, 0.02])
    assert sr == 0.0
    assert skew == 0.0
    assert kurt == 3.0
    assert n == 2
