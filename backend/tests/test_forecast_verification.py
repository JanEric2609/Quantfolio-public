"""Unit tests for foundation/forecast_verification.py (pure statistics, no DB)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.foundation import forecast_verification as fv


# ---------------------------------------------------------------------------
# Clopper-Pearson
# ---------------------------------------------------------------------------


def test_hit_rate_ci_zero_hits_matches_closed_form():
    lo, hi = fv.hit_rate_ci(0, 10, level=0.9)
    assert lo == 0.0
    # With k = 0 the upper bound is 1 - (alpha/2)^(1/n).
    assert hi == pytest.approx(1 - 0.05 ** (1 / 10), abs=1e-9)


def test_hit_rate_ci_all_hits_matches_closed_form():
    lo, hi = fv.hit_rate_ci(10, 10, level=0.9)
    assert hi == 1.0
    assert lo == pytest.approx(0.05 ** (1 / 10), abs=1e-9)


def test_hit_rate_ci_known_value_and_symmetry():
    lo, hi = fv.hit_rate_ci(5, 10, level=0.9)
    assert lo == pytest.approx(0.2224, abs=5e-4)
    assert hi == pytest.approx(0.7776, abs=5e-4)
    assert lo + hi == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("hits,n", [(5, 8), (6, 10), (0, 7), (7, 7), (33, 120)])
@pytest.mark.parametrize("level", [0.8, 0.9, 0.95])
def test_hit_rate_ci_agrees_with_scipy_exact_interval(hits, n, level):
    from scipy import stats

    ref = stats.binomtest(hits, n).proportion_ci(level, method="exact")
    lo, hi = fv.hit_rate_ci(hits, n, level=level)
    assert lo == pytest.approx(ref.low, abs=1e-9)
    assert hi == pytest.approx(ref.high, abs=1e-9)


@pytest.mark.parametrize("hits,n", [(1, 3), (5, 8), (6, 10), (70, 100), (0, 1), (1, 1)])
def test_hit_rate_ci_contains_point_estimate(hits, n):
    lo, hi = fv.hit_rate_ci(hits, n)
    assert lo <= hits / n <= hi


def test_hit_rate_ci_gets_narrower_with_more_data():
    lo_a, hi_a = fv.hit_rate_ci(6, 10)
    lo_b, hi_b = fv.hit_rate_ci(60, 100)
    assert (hi_b - lo_b) < (hi_a - lo_a)


def test_hit_rate_ci_none_without_calls_and_validates_input():
    assert fv.hit_rate_ci(0, 0) is None
    with pytest.raises(ValueError):
        fv.hit_rate_ci(5, 3)
    with pytest.raises(ValueError):
        fv.hit_rate_ci(1, 3, level=1.0)


def test_coverage_reports_k_n_and_exact_interval():
    cov = fv.coverage([True] * 7 + [False] * 3, level=0.8)
    assert (cov.k, cov.n, cov.rate, cov.nominal) == (7, 10, 0.7, 0.8)
    assert cov.ci_low == pytest.approx(fv.hit_rate_ci(7, 10)[0])
    assert cov.ci_high == pytest.approx(fv.hit_rate_ci(7, 10)[1])
    assert cov.ci_low <= cov.rate <= cov.ci_high


def test_coverage_empty():
    cov = fv.coverage([], level=0.8)
    assert cov.n == 0 and cov.rate is None and cov.ci_low is None and cov.ci_high is None


# ---------------------------------------------------------------------------
# Brier score and skill
# ---------------------------------------------------------------------------


def test_brier_score_hand_computed():
    # (0.3^2 + 0.3^2) / 2
    assert fv.brier_score([0.7, 0.3], [1, 0]) == pytest.approx(0.09)
    assert fv.brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert fv.brier_score([0.5], [1]) == pytest.approx(0.25)


def test_brier_score_validates_input():
    with pytest.raises(ValueError):
        fv.brier_score([0.5], [1, 0])
    with pytest.raises(ValueError):
        fv.brier_score([], [])
    with pytest.raises(ValueError):
        fv.brier_score([1.2], [1])


def test_brier_skill_against_fixed_reference_is_hand_computed():
    res = fv.brier_skill([0.7, 0.3], [1, 0], p_ref=0.5)
    # 1 - 0.09 / 0.25
    assert res.skill == pytest.approx(0.64)
    assert res.brier == pytest.approx(0.09)
    assert res.brier_ref == pytest.approx(0.25)
    assert res.n == 2


def test_brier_skill_default_reference_is_the_base_rate():
    y = [1, 1, 0, 0, 0, 0, 0, 0, 0, 0]
    res = fv.brier_skill([0.2] * 10, y)
    # Forecasting exactly the base rate is no better and no worse than it.
    assert res.skill == pytest.approx(0.0, abs=1e-12)
    assert res.brier_ref == pytest.approx(0.2 * 0.8)


def test_brier_skill_array_reference():
    res = fv.brier_skill([0.9, 0.1], [1, 0], p_ref=[0.5, 0.5])
    assert res.skill == pytest.approx(1 - 0.01 / 0.25)


def test_brier_skill_undefined_when_reference_is_perfect():
    res = fv.brier_skill([0.6] * 20, [1] * 20)
    assert res.skill is None and res.ci_low is None and res.ci_high is None


def test_brier_skill_ci_is_none_below_minimum_calls():
    res = fv.brier_skill([0.8, 0.2] * 7, [1, 0] * 7)  # 14 calls
    assert res.skill is not None
    assert res.ci_low is None and res.ci_high is None


def _informative_sample(n: int, seed: int = 1):
    rng = np.random.default_rng(seed)
    p = rng.choice([0.25, 0.5, 0.75], size=n)
    y = (rng.random(n) < p).astype(int)
    return p, y


def test_brier_skill_ci_contains_point_estimate_and_is_deterministic():
    p, y = _informative_sample(120)
    a = fv.brier_skill(p, y, seed=3)
    b = fv.brier_skill(p, y, seed=3)
    assert a == b
    assert a.ci_low is not None and a.ci_high is not None
    assert a.ci_low <= a.skill <= a.ci_high
    # Informative forecasts beat the base rate.
    assert a.skill > 0


def test_brier_skill_ci_narrows_with_sample_size():
    p_small, y_small = _informative_sample(40, seed=2)
    p_big, y_big = _informative_sample(800, seed=2)
    small = fv.brier_skill(p_small, y_small)
    big = fv.brier_skill(p_big, y_big)
    assert (big.ci_high - big.ci_low) < (small.ci_high - small.ci_low)


def test_brier_skill_negative_for_overconfident_noise():
    rng = np.random.default_rng(5)
    y = (rng.random(300) < 0.5).astype(int)
    p = np.where(rng.random(300) < 0.5, 0.9, 0.1)  # confident and unrelated to y
    res = fv.brier_skill(p, y)
    assert res.skill < 0
    assert res.ci_high < 0.1


# ---------------------------------------------------------------------------
# Block bootstrap
# ---------------------------------------------------------------------------


def test_block_length_rule():
    assert fv.block_length(1) == 1
    assert fv.block_length(8) == 2
    assert fv.block_length(27) == 3
    assert fv.block_length(1000) == 10


def test_mean_ci_bootstrap_none_below_minimum_calls():
    assert fv.mean_ci_bootstrap([0.01] * 14) is None
    assert fv.mean_ci_bootstrap([]) is None


def test_mean_ci_bootstrap_contains_mean_and_is_deterministic():
    rng = np.random.default_rng(11)
    x = rng.normal(0.01, 0.05, size=80)
    ci = fv.mean_ci_bootstrap(x, level=0.9, seed=4)
    assert ci == fv.mean_ci_bootstrap(x, level=0.9, seed=4)
    lo, hi = ci
    assert lo <= x.mean() <= hi
    # Roughly +/- 1.645 standard errors.
    se = x.std(ddof=1) / math.sqrt(len(x))
    assert (hi - lo) == pytest.approx(2 * 1.645 * se, rel=0.35)


def test_mean_ci_bootstrap_wider_at_higher_level():
    rng = np.random.default_rng(12)
    x = rng.normal(0.0, 0.05, size=60)
    lo90, hi90 = fv.mean_ci_bootstrap(x, level=0.9, seed=1)
    lo99, hi99 = fv.mean_ci_bootstrap(x, level=0.99, seed=1)
    assert (hi99 - lo99) > (hi90 - lo90)


def test_mean_ci_bootstrap_constant_series_is_a_point():
    lo, hi = fv.mean_ci_bootstrap([0.02] * 30)
    assert lo == pytest.approx(0.02) and hi == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# E-process
# ---------------------------------------------------------------------------


def test_e_process_hand_computed_first_steps():
    proc = fv.e_process_bernoulli([1, 1], p0=0.5)
    # Bet 1 is 0 (prior mean 0.5 == p0): E1 = 1.
    # Bet 2: mu = 0.75, sigma2 = 0.3125 / 2, edge 0.25 -> lambda = 1.142857,
    # capped at 0.5 / p0 = 1.0, so E2 = 1 * (1 + 1.0 * 0.5) = 1.5.
    assert proc.path[0] == pytest.approx(1.0)
    assert proc.path[1] == pytest.approx(1.5)
    assert proc.final == pytest.approx(1.5)
    assert proc.n == 2


def test_e_process_all_hits_grows_past_both_thresholds():
    proc = fv.e_process_bernoulli([1] * 40, p0=0.5)
    assert proc.final > fv.STRONG_EVIDENCE_THRESHOLD
    assert proc.crossed(fv.EVIDENCE_THRESHOLD)
    assert proc.crossed(fv.STRONG_EVIDENCE_THRESHOLD)
    assert np.all(np.diff(proc.path) >= 0)


def test_e_process_all_misses_places_no_bet_and_never_crosses():
    # The bet is clipped at zero while the observed rate is below p0, so the
    # skill e-process sits at 1 ("no evidence") instead of going negative.
    proc = fv.e_process_bernoulli([0] * 40, p0=0.5)
    assert proc.final == pytest.approx(1.0)
    assert not proc.crossed(fv.EVIDENCE_THRESHOLD)


def test_e_process_loses_when_a_good_start_is_followed_by_misses():
    proc = fv.e_process_bernoulli([1] * 10 + [0] * 10, p0=0.5)
    assert proc.final < proc.max_value
    assert np.all(proc.path > 0)


def test_e_process_stays_positive_even_with_extreme_bets():
    proc = fv.e_process_bernoulli([1] * 25 + [0] * 25, p0=0.2)
    assert np.all(proc.path > 0)


def test_e_process_coin_flips_stay_below_threshold_on_seeded_run():
    rng = np.random.default_rng(0)
    x = rng.integers(0, 2, size=400)
    proc = fv.e_process_bernoulli(x, p0=0.5)
    assert proc.max_value < fv.EVIDENCE_THRESHOLD


def test_e_process_type_one_error_is_controlled_under_the_null():
    # Ville: P(max E >= 20) <= 5 % when H0 holds. 300 fair-coin sequences of
    # 150 calls; allow generous Monte-Carlo slack.
    rng = np.random.default_rng(123)
    crossings = sum(
        fv.e_process_bernoulli(rng.integers(0, 2, size=150), p0=0.5).crossed(fv.EVIDENCE_THRESHOLD)
        for _ in range(300)
    )
    assert crossings / 300 <= 0.05


def test_e_process_detects_real_skill_eventually():
    rng = np.random.default_rng(7)
    x = (rng.random(400) < 0.65).astype(int)
    assert fv.e_process_bernoulli(x, p0=0.5).crossed(fv.EVIDENCE_THRESHOLD)


def test_running_max_is_monotone_and_at_least_one():
    proc = fv.e_process_bernoulli([0, 0, 1, 1, 1, 0, 1], p0=0.5)
    assert np.all(np.diff(proc.running_max) >= 0)
    assert np.all(proc.running_max >= 1.0)
    assert proc.running_max[-1] >= proc.path.max() - 1e-12


def test_e_process_empty_is_neutral():
    proc = fv.e_process_bernoulli([], p0=0.5)
    assert proc.n == 0 and proc.final == 1.0 and proc.max_value == 1.0


def test_e_process_harm_grows_for_misses_and_not_for_hits():
    assert fv.e_process_harm([0] * 40, p0=0.5).crossed(fv.EVIDENCE_THRESHOLD)
    assert not fv.e_process_harm([1] * 40, p0=0.5).crossed(fv.EVIDENCE_THRESHOLD)


def test_e_process_validates_input():
    with pytest.raises(ValueError):
        fv.e_process_bernoulli([1, 0], p0=0.0)
    with pytest.raises(ValueError):
        fv.e_process_bernoulli([2], p0=0.5)


# ---------------------------------------------------------------------------
# evidence_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,e_skill,e_harm,expected",
    [
        (5, 1.0, 1.0, "too_early"),
        (19, 3.0, 2.0, "too_early"),
        (20, 3.0, 2.0, "no_evidence"),
        (200, 1.4, 0.9, "no_evidence"),
        (30, 25.0, 1.0, "skill"),
        (8, 25.0, 1.0, "skill"),  # crossing wins even below n_min
        (30, 1.0, 40.0, "harm"),
        (30, 25.0, 40.0, "harm"),  # sign changed: warning beats reassurance
    ],
)
def test_evidence_state(n, e_skill, e_harm, expected):
    assert fv.evidence_state(n, e_skill, e_harm) == expected


def test_evidence_state_threshold_is_inclusive_and_n_min_configurable():
    assert fv.evidence_state(50, 20.0, 1.0) == "skill"
    assert fv.evidence_state(50, 19.99, 1.0) == "no_evidence"
    assert fv.evidence_state(50, 1.0, 1.0, n_min=100) == "too_early"


# ---------------------------------------------------------------------------
# calls_needed
# ---------------------------------------------------------------------------


def test_calls_needed_matches_the_plan_numbers():
    assert fv.calls_needed(0.6) == 153
    assert fv.calls_needed(0.55) == 617


def test_calls_needed_grows_as_the_edge_shrinks_and_with_power():
    assert fv.calls_needed(0.7) < fv.calls_needed(0.6) < fv.calls_needed(0.55)
    assert fv.calls_needed(0.6, power=0.9) > fv.calls_needed(0.6, power=0.8)


def test_calls_needed_validates_input():
    with pytest.raises(ValueError):
        fv.calls_needed(0.5)
    with pytest.raises(ValueError):
        fv.calls_needed(0.4)
    with pytest.raises(ValueError):
        fv.calls_needed(1.0)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_spiegelhalter_z_hand_computed():
    # terms: (1-.8)(1-1.6) = -0.12 and (0-.2)(1-.4) = -0.12 -> -0.24;
    # denominator sqrt(2 * 0.36 * 0.16) = 0.33941.
    assert fv.spiegelhalter_z([0.8, 0.2], [1, 0]) == pytest.approx(-1 / math.sqrt(2), abs=1e-9)


def test_spiegelhalter_z_small_for_calibrated_large_for_overconfident():
    rng = np.random.default_rng(21)
    p = rng.uniform(0.1, 0.9, size=1000)
    calibrated = (rng.random(1000) < p).astype(int)
    z_calibrated = fv.spiegelhalter_z(p, calibrated)
    assert abs(z_calibrated) < 3.0
    # Same stated probabilities, but the truth is a coin flip.
    coin = (rng.random(1000) < 0.5).astype(int)
    assert abs(fv.spiegelhalter_z(np.where(p > 0.5, 0.9, 0.1), coin)) > 5.0


def test_spiegelhalter_z_undefined_at_half():
    assert fv.spiegelhalter_z([0.5, 0.5, 0.5], [1, 0, 1]) is None


def test_reliability_bins_hand_computed():
    bins = fv.reliability_bins([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], bins=2)
    assert bins == [
        {"p_mean": pytest.approx(0.15), "hit_rate": 0.0, "n": 2},
        {"p_mean": pytest.approx(0.85), "hit_rate": 1.0, "n": 2},
    ]


def test_reliability_bins_are_ordered_and_conserve_counts():
    rng = np.random.default_rng(3)
    p = rng.uniform(0.3, 0.8, size=47)
    y = (rng.random(47) < p).astype(int)
    bins = fv.reliability_bins(p, y, bins=5)
    assert len(bins) == 5
    assert sum(b["n"] for b in bins) == 47
    assert [b["p_mean"] for b in bins] == sorted(b["p_mean"] for b in bins)
    assert sum(b["hit_rate"] * b["n"] for b in bins) == pytest.approx(y.sum())


def test_reliability_bins_fewer_points_than_bins_and_empty():
    assert len(fv.reliability_bins([0.4, 0.6], [0, 1], bins=5)) == 2
    assert fv.reliability_bins([], []) == []


def test_hac_interval_widens_with_positive_autocorrelation():
    rng = np.random.default_rng(1)
    shocks = rng.normal(0, 0.02, 200)
    smooth = np.convolve(shocks, np.ones(5) / 5, mode="valid")  # overlapping windows
    naive = fv.mean_ci_hac(smooth, 0)
    hac = fv.mean_ci_hac(smooth, 4)
    assert naive is not None and hac is not None
    assert (hac[1] - hac[0]) > 1.5 * (naive[1] - naive[0])
    assert fv.mean_ci_hac([0.01] * 7, 2) is None  # below 8 observations


def test_e_bh_needs_more_than_one_over_alpha_when_there_are_several_tests():
    assert fv.e_bh([25.0], 0.05) == [True]
    assert fv.e_bh([25.0, 1.0, 1.0, 1.0], 0.05) == [False, False, False, False]
    assert fv.e_bh([90.0, 45.0, 1.0, 1.0], 0.05) == [True, True, False, False]


# ---------------------------------------------------------------------------
# Lag-h e-process (overlapping outcome windows) and the simulated time to know
# ---------------------------------------------------------------------------


def test_lag_h_with_h_one_is_the_plain_e_process():
    x = np.array([1, 1, 0, 1, 1, 0, 1, 1, 1, 0], dtype=float)
    plain = fv.e_process_bernoulli(x, 0.5)
    lag1 = fv.e_process_lag_h(x, 0.5, 1)
    assert np.allclose(plain.path, lag1.path)
    assert fv.e_process_lag_h(np.array([]), 0.5, 5).n == 0


def test_lag_h_path_is_the_average_of_the_interleaved_subsequence_e_processes():
    rng = np.random.default_rng(3)
    x = (rng.random(40) < 0.6).astype(float)
    h = 4
    lag = fv.e_process_lag_h(x, 0.5, h)
    subs = [fv.e_process_bernoulli(x[k::h], 0.5).path for k in range(h)]
    for t in (0, 5, 17, 39):
        expected = np.mean([subs[k][(t - k) // h] if t >= k else 1.0 for k in range(h)])
        assert lag.path[t] == pytest.approx(expected)
    # Harm direction mirrors it.
    assert fv.e_process_lag_h_harm(x, 0.5, h).final == pytest.approx(fv.e_process_lag_h(1 - x, 0.5, h).final)


def test_vectorised_simulation_core_matches_the_scalar_e_process():
    x = (np.random.default_rng(1).random(120) < 0.6).astype(float)
    assert np.allclose(fv._agrapa_paths(x[:, None], 0.5)[:, 0], fv.e_process_bernoulli(x, 0.5).path)


def test_lag_h_e_process_is_valid_under_the_null_with_overlapping_windows():
    """Hit flags built from overlapping h-day sums (each flag shares h-1 shocks with its
    neighbour) have P(hit) = 0.5 but are strongly serially dependent. The lag-h average
    must cross 1/alpha in at most alpha of runs (plus Monte-Carlo tolerance)."""
    h, n, runs, alpha = 5, 150, 800, 0.05
    rng = np.random.default_rng(11)
    shocks = rng.standard_normal((runs, n + h - 1))
    csum = np.cumsum(shocks, axis=1)
    window = csum[:, h - 1:] - np.concatenate([np.zeros((runs, 1)), csum[:, :-h]], axis=1)
    flags = (window > 0).astype(float)
    assert flags.shape == (runs, n)
    crossed = sum(fv.e_process_lag_h(row, 0.5, h).crossed(1.0 / alpha) for row in flags)
    assert crossed / runs <= alpha + 0.025


def test_issue_days_needed_reference_values_and_speed():
    import time

    fv.issue_days_needed.cache_clear()
    start = time.perf_counter()
    default = fv.issue_days_needed(0.55, 0.5, 4, 0.05, 21)
    assert time.perf_counter() - start < 0.2
    assert default == 9783  # seeded: median issue days to e >= K/alpha = 80
    assert fv.issue_days_needed(0.55, 0.5, 4, 0.05, 21) == default  # cached, deterministic
    assert fv.issue_days_needed(0.60, 0.5, 4, 0.05, 21) == 2105
    assert fv.issue_days_needed(0.55, 0.5, 4, 0.05, 1) == 1206  # no overlap: a plain e-process
    # A stronger edge and a smaller family both need fewer days.
    assert fv.issue_days_needed(0.60, 0.5, 4, 0.05, 21) < default
    assert fv.issue_days_needed(0.55, 0.5, 2, 0.05, 21) < default
    # Not reachable within the cap: reported as None, never a made-up number.
    assert fv.issue_days_needed(0.505, 0.5, 4, 0.05, 21, n_max=500) is None


def test_mean_e_process_is_valid_under_the_null_with_overlapping_windows():
    """Per-date excess returns that are overlapping h-day sums of zero-mean shocks (so each
    value shares h-1 shocks with its neighbour) have mean 0 and are strongly dependent.
    Both directions of the lag-h excess-return e-process must cross 1/alpha in at most
    alpha of runs (plus Monte-Carlo tolerance)."""
    h, n, runs, alpha, clip, sd = 5, 200, 600, 0.05, 0.20, 0.05
    rng = np.random.default_rng(5)
    shocks = rng.standard_normal((runs, n + h - 1)) * sd / math.sqrt(h)
    csum = np.cumsum(shocks, axis=1)
    x = csum[:, h - 1:] - np.concatenate([np.zeros((runs, 1)), csum[:, :-h]], axis=1)
    for harm in (False, True):
        crossed = sum(fv.e_process_mean(row, h, clip, sd, harm=harm).crossed(1.0 / alpha) for row in x)
        assert crossed / runs <= alpha + 0.025


def test_mean_e_process_moves_in_the_right_direction_and_clips_before_betting():
    up = np.full(120, 0.03)
    assert fv.e_process_mean(up, 1, 0.2, 0.05).final > 20
    assert fv.e_process_mean(up, 1, 0.2, 0.05, harm=True).final <= 1.0
    assert fv.e_process_mean(-up, 1, 0.2, 0.05, harm=True).final > 20
    # A freak +500 % date counts as +clip, no more: same path as the clipped series.
    freak = np.array([0.01, 5.0, 0.02, -0.01])
    clipped = np.array([0.01, 0.2, 0.02, -0.01])
    assert np.allclose(fv.e_process_mean(freak, 2, 0.2, 0.05).path, fv.e_process_mean(clipped, 2, 0.2, 0.05).path)
    with pytest.raises(ValueError):
        fv.e_process_mean(up, 1, 0.0, 0.05)


def test_issue_days_needed_mean_reference_values():
    import time

    fv.issue_days_needed_mean.cache_clear()
    start = time.perf_counter()
    default = fv.issue_days_needed_mean(0.01, 0.05, 0.20, 4, 0.05, 21)
    assert time.perf_counter() - start < 0.3
    assert default == 4132  # +1 % per date, 5 % noise, K=4 -> e >= 80, h=21
    assert fv.issue_days_needed_mean(0.01, 0.05, 0.20, 4, 0.05, 1) == 262
    assert fv.issue_days_needed_mean(0.02, 0.05, 0.20, 4, 0.05, 21) == 1991
    assert fv.issue_days_needed_mean(0.01, 0.05, 0.20, 2, 0.05, 21) < default
    assert fv.issue_days_needed_mean(0.0005, 0.05, 0.20, 4, 0.05, 21, n_max=500) is None
    with pytest.raises(ValueError):
        fv.issue_days_needed_mean(0.0, 0.05, 0.20)


# ---------------------------------------------------------------------------
# Schedule-aware chains (uneven issue dates)
# ---------------------------------------------------------------------------


def _trading_days(n: int):
    from datetime import date, timedelta

    out, d = [], date(2026, 3, 2)  # a Monday
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _n_chains(days, h=21) -> int:
    return max(fv.chain_partition(days, h)) + 1


def test_chain_partition_daily_weekly_and_a_burst():
    from datetime import date, timedelta

    daily = _trading_days(120)
    assert _n_chains(daily) == 21
    # Daily issuing is exactly the index-mod-h interleaving.
    assert fv.chain_partition(daily, 21) == [i % 21 for i in range(120)]
    weekly = [date(2026, 3, 2) + timedelta(weeks=i) for i in range(40)]
    assert _n_chains(weekly) == 5
    assert _n_chains(daily[:1]) == 1
    # A burst: 10 dates on consecutive days, then a quiet stretch of weekly dates.
    burst = daily[:10] + [date(2026, 5, 4) + timedelta(weeks=i) for i in range(10)]
    chains = fv.chain_partition(burst, 21)
    assert len(set(chains[:10])) == 10  # every date of the burst needs its own chain
    assert max(chains) + 1 <= 10 + 1
    # Inside a chain, windows never overlap.
    import numpy as np

    for c in set(chains):
        ds = [d for d, k in zip(burst, chains, strict=True) if k == c]
        for a, b in zip(ds, ds[1:], strict=False):
            assert np.busday_count(a, b) >= 21


def test_effective_lag_follows_the_cadence_and_defaults_to_daily():
    from datetime import date, timedelta

    weekly = [date(2026, 3, 2) + timedelta(weeks=i) for i in range(10)]
    assert fv.effective_lag(weekly, 21) == (5, 5.0)
    assert fv.effective_lag(_trading_days(30), 21) == (21, 1.0)
    assert fv.effective_lag(weekly[:4], 21) == (21, 1.0)  # fewer than 5 dates: assume daily


def test_chain_e_process_equals_lag_h_for_daily_issuing_and_checks_shapes():
    x = (np.random.default_rng(2).random(60) < 0.6).astype(float)
    daily = _trading_days(60)
    a = fv.e_process_chains(x, fv.chain_partition(daily, 7), 0.5, n_chains=7)
    assert np.allclose(a.path, fv.e_process_lag_h(x, 0.5, 7).path)
    with pytest.raises(ValueError):
        fv.e_process_chains(x, [0, 1], 0.5)


def test_mean_e_process_is_valid_on_an_irregular_overlapping_schedule():
    """Issue dates in bursts and gaps; the value on each date is the mean of the daily
    shocks over the next h trading days (so overlapping dates share shocks). Zero-mean
    shocks: the chain-averaged e-process must cross 1/alpha in <= alpha of runs, both ways."""
    from datetime import date, timedelta

    h, runs, alpha, clip, sd = 6, 500, 0.05, 0.20, 0.05
    # trading-day offsets of the issue dates: a daily burst, a gap, then every third day
    offsets = list(range(0, 12)) + list(range(30, 90, 3)) + list(range(100, 106))
    days = [date(2026, 3, 2) + timedelta(days=0)]
    base = _trading_days(offsets[-1] + 1)
    days = [base[o] for o in offsets]
    chains = fv.chain_partition(days, h)
    rng = np.random.default_rng(9)
    shocks = rng.standard_normal((runs, offsets[-1] + h + 1)) * sd / math.sqrt(h)
    csum = np.concatenate([np.zeros((runs, 1)), np.cumsum(shocks, axis=1)], axis=1)
    x = np.stack([csum[:, o + h] - csum[:, o] for o in offsets], axis=1)
    for harm in (False, True):
        crossed = sum(
            fv.e_process_mean(row, h, clip, sd, harm=harm, chains=chains).crossed(1.0 / alpha) for row in x
        )
        assert crossed / runs <= alpha + 0.025


def test_issue_days_needed_mean_reference_values_for_the_weekly_cadence():
    assert fv.issue_days_needed_mean(0.01, 0.05, 0.20, 4, 0.05, 5) == 1042


# ---------------------------------------------------------------------------
# Adaptive conformal correction of stated ranges (ADR 0018 §6)
# ---------------------------------------------------------------------------


def _weekly_ranges(n_weeks: int, per_date: int, spread: float, seed: int = 7) -> list:
    from datetime import date, timedelta

    rng = np.random.default_rng(seed)
    out = []
    start = date(2025, 1, 6)
    for w in range(n_weeks):
        issued = start + timedelta(days=7 * w)
        matured = issued + timedelta(days=30)
        for x in rng.normal(0.0, spread, per_date):
            out.append(fv.RangeOutcome(issued=issued, matured=matured, low=-0.05, high=0.05, outcome=float(x)))
    return out


def test_aci_inactive_before_thirty_matured_weeks():
    aci = fv.aci_coverage(_weekly_ranges(20, 5, 0.04), 0.8)
    assert not aci.active and aci.widen_now is None and aci.corrected is None
    assert aci.matured_weeks == 20 and aci.alpha_now == pytest.approx(0.2)
    assert len(aci.per_date) == 20


def test_aci_widens_a_band_that_is_too_narrow():
    # The stated band is [-5 %, +5 %] but the outcomes have sd 8 %: raw
    # coverage is about 47 %, far below the nominal 80 %.
    aci = fv.aci_coverage(_weekly_ranges(120, 10, 0.08), 0.8)
    assert aci.active and aci.widen_now is not None and aci.widen_now > 0
    assert aci.raw_same_dates is not None and aci.corrected is not None
    assert aci.raw_same_dates.rate < 0.6
    assert aci.corrected.rate > aci.raw_same_dates.rate
    assert abs(aci.corrected.rate - 0.8) < 0.08


def test_aci_narrows_a_band_that_is_too_wide():
    aci = fv.aci_coverage(_weekly_ranges(120, 10, 0.01), 0.8)
    assert aci.active and aci.widen_now is not None and aci.widen_now < 0
    assert aci.corrected is not None and aci.corrected.rate < 0.95


def test_aci_never_reads_an_outcome_that_matured_after_the_issue_date():
    from datetime import date

    # Thirty early weeks matured long before; one late date whose own outcome
    # is wildly outside. Its correction must not depend on its own outcome.
    base = _weekly_ranges(40, 5, 0.04)
    late = date(2026, 6, 1)
    a = fv.aci_coverage([*base, fv.RangeOutcome(late, date(2026, 7, 1), -0.05, 0.05, 0.0)], 0.8)
    b = fv.aci_coverage([*base, fv.RangeOutcome(late, date(2026, 7, 1), -0.05, 0.05, 5.0)], 0.8)
    assert a.corrected is not None and b.corrected is not None
    assert a.corrected.n == b.corrected.n
    assert a.raw_same_dates is not None and b.raw_same_dates is not None
    assert a.corrected.k - b.corrected.k == a.raw_same_dates.k - b.raw_same_dates.k == 1


def test_aci_point_ranges_and_a_historical_cutoff():
    from datetime import date

    point = fv.RangeOutcome(date(2025, 1, 6), date(2025, 2, 6), 0.01, 0.01, 0.01)
    assert fv.aci_coverage([point], 0.8).per_date == [(date(2025, 1, 6), 1, 1)]

    ranges = _weekly_ranges(60, 5, 0.08)
    early = fv.aci_coverage(ranges, 0.8, today=date(2025, 6, 1))
    assert all(d < date(2025, 6, 1) for d, _, _ in early.per_date)
    assert early.matured_weeks < fv.aci_coverage(ranges, 0.8).matured_weeks
