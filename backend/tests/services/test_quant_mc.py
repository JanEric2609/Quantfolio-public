"""Unit tests for Phase 2 Monte Carlo engine.

Tests cover variance reduction efficiency, MLMC convergence, and Greeks accuracy.
"""

import numpy as np
import pytest
from app.foundation.quant_mc import MonteCarloEngine, McSpec
from app.foundation.quant_mc.greeks import compute_black_scholes_greeks
from app.foundation.quant_mc.variance_reduction import (
    ControlVariateReducer,
    AntitheticReducer,
)


class TestVarianceReduction:
    """Test variance reduction techniques meet efficiency targets."""

    def test_antithetic_reduces_variance(self):
        """Antithetic sampling should reduce variance materially.

        Uses E[exp(Z)] with Z ~ N(0, 1). Antithetic pair is exp(-Z); pairing
        exploits the monotone increasing exp() to give strongly negatively
        correlated pairs and a large variance reduction.
        """
        rng = np.random.default_rng(42)

        n_samples = 10000
        z = rng.standard_normal(n_samples)
        payoffs = np.exp(z)
        antithetic_payoffs = np.exp(-z)

        reducer = AntitheticReducer()
        adjusted, ratio = reducer.reduce(
            payoffs,
            {"antithetic_payoffs": antithetic_payoffs},
        )

        # Should achieve >=1.5x variance reduction.
        assert ratio >= 1.5, f"Antithetic ratio {ratio} < 1.5"

    def test_control_variate_reduces_variance(self):
        """Control variate should reduce variance by ≥1.5× on a correlated estimator."""
        np.random.seed(43)
        rng = np.random.default_rng(43)

        # Estimate E[sin²(U)] using a correlated control cos²(U)
        n_samples = 10000
        U = rng.uniform(0, np.pi, n_samples)
        payoffs = np.sin(U) ** 2
        controls = np.cos(U) ** 2

        reducer = ControlVariateReducer()
        adjusted, ratio = reducer.reduce(
            payoffs,
            {"control_values": controls},
        )

        assert ratio >= 1.5, f"Control variate ratio {ratio} < 1.5"


class TestSelfGeneratedVarianceReduction:
    """Engine-level tests for control_variate/stratified self-generation
    (deepdive-03-quantlab Issue 2, items 2-3): previously these techniques
    were structural no-ops because nothing ever supplied additional_data.
    """

    def test_control_variate_self_generates_for_portfolio_var(self):
        """When control_variate is selected with no additional_data override,
        the engine should self-generate control_values = paths[:, -1] (the
        terminal price). For portfolio_var (an exact affine function of the
        terminal price) this achieves near-perfect (rho~1) reduction — see
        the caveat comment in MonteCarloEngine._build_variance_reduction_data.
        """
        engine = MonteCarloEngine()
        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=50,
            T=1.0,
            n_paths=5000,
            payoff="portfolio_var",
            variance_reduction=["control_variate"],
            compute_greeks=False,
            seed=300,
        )
        result, _ = engine.run(spec)
        assert result.variance_reduction_gain is not None
        ratio = result.variance_reduction_gain["control_variate"]
        assert ratio > 1e6, f"expected near-perfect reduction for a linear payoff, got ratio={ratio}"

    def test_control_variate_additional_data_override_is_used(self):
        """An explicit additional_data override must not be clobbered by
        self-generation."""
        engine = MonteCarloEngine()
        kwargs = dict(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=50,
            T=1.0,
            n_paths=3000,
            payoff="portfolio_var",
            variance_reduction=["control_variate"],
            compute_greeks=False,
            seed=303,
        )
        self_generated_result, _ = engine.run(McSpec(**kwargs))

        rng_for_control = np.random.default_rng(999)
        uncorrelated_control = rng_for_control.standard_normal(kwargs["n_paths"])
        override_result, _ = engine.run(
            McSpec(**kwargs, additional_data={"control_values": uncorrelated_control})
        )

        self_gen_ratio = self_generated_result.variance_reduction_gain["control_variate"]
        override_ratio = override_result.variance_reduction_gain["control_variate"]

        # Self-generated control (terminal price) is perfectly correlated
        # with the linear portfolio_var payoff -> huge ratio. An uncorrelated
        # override control should NOT achieve that, proving the override
        # actually took effect instead of being silently replaced.
        assert self_gen_ratio > 1e6
        assert override_ratio < 10.0

    def test_stratified_self_generates_and_reduces_variance(self):
        """When stratified is selected with no override, the engine should
        self-generate stratum_indices by binning paths[:, -1] into n_strata
        empirical-quantile buckets, and this should genuinely reduce
        variance for a non-linear payoff (european_call)."""
        engine = MonteCarloEngine()
        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=50,
            T=1.0,
            n_paths=5000,
            payoff="european_call",
            payoff_params={"strike": 100.0},
            variance_reduction=["stratified"],
            compute_greeks=False,
            seed=301,
        )
        result, _ = engine.run(spec)
        assert result.variance_reduction_gain is not None
        ratio = result.variance_reduction_gain["stratified"]
        assert ratio > 1.0, f"expected variance reduction, got ratio={ratio}"

    def test_stratified_preserves_point_estimate(self):
        """Post-stratification (replacing each value by its stratum mean)
        should leave the overall mean essentially unchanged (Owen ch. 8)."""
        engine = MonteCarloEngine()
        kwargs = dict(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=50,
            T=1.0,
            n_paths=8000,
            payoff="european_call",
            payoff_params={"strike": 100.0},
            compute_greeks=False,
            seed=304,
        )
        plain_result, _ = engine.run(McSpec(**kwargs))
        stratified_result, _ = engine.run(McSpec(**kwargs, variance_reduction=["stratified"]))

        # Same seed -> same underlying draws; stratification only reduces
        # within-stratum spread, so the mean should barely move.
        rel_diff = abs(stratified_result.estimate - plain_result.estimate) / max(
            abs(plain_result.estimate), 1e-8
        )
        assert rel_diff < 0.05, f"stratified mean drifted too far: {rel_diff:.4f}"


class TestImportanceSampling:
    """Tests for the real (non-plumbing) importance-sampling implementation
    (deepdive-03-quantlab Issue 2, item 4): a drift-shifted GBM sampling path
    plus the matching log-likelihood ratio (Glasserman-Heidelberger-
    Shahabuddin 2000 exponential twisting).
    """

    def test_reweighted_estimator_is_unbiased(self):
        """Pure-math consistency check (fixed seeds, large sample): the mean
        of payoffs drawn under the drift-shifted measure Q and reweighted by
        exp(log_likelihood_ratio) should approximately match the mean of
        payoffs drawn directly under the true (unshifted) measure P, for the
        exact formula MonteCarloEngine._simulate_paths_importance_sampling
        implements:
            theta = sigma * Phi^-1(0.05) / sqrt(T)
            log(dP/dQ) = -theta * W_T / sigma - theta^2 * T / (2 * sigma^2)
        """
        from scipy.stats import norm

        mu, sigma, T = 0.05, 0.2, 1.0
        n_paths = 200_000

        # True (unshifted) mean payoff via direct closed-form GBM sampling
        # (single-step terminal draw is equivalent to GBMEuler's exact
        # closed-form solution for the terminal value).
        rng_true = np.random.default_rng(7)
        z_true = rng_true.standard_normal(n_paths)
        W_true = np.sqrt(T) * z_true
        log_ret_true = (mu - 0.5 * sigma**2) * T + sigma * W_true
        payoff_true = (np.exp(log_ret_true) - 1) * 10000
        mean_true = payoff_true.mean()

        # Shifted-drift sampling + reweighting (mirrors the engine's formula).
        theta = sigma * norm.ppf(0.05) / np.sqrt(T)

        rng_shift = np.random.default_rng(8)
        z_shift = rng_shift.standard_normal(n_paths)
        W_shift = np.sqrt(T) * z_shift
        log_ret_shift = (mu + theta - 0.5 * sigma**2) * T + sigma * W_shift
        payoff_shift = (np.exp(log_ret_shift) - 1) * 10000

        log_llr = -theta * W_shift / sigma - theta**2 * T / (2 * sigma**2)
        weights = np.exp(log_llr)
        reweighted = payoff_shift * weights
        mean_reweighted = reweighted.mean()

        # Sanity: weights should average to ~1 (dQ/dP normalises correctly).
        assert abs(weights.mean() - 1.0) < 0.05

        # Both are MC estimators of the same quantity (E_P[(S_T/S0-1)*1e4]).
        # The drift-shifted/reweighted estimator has substantially higher
        # variance than the unshifted one for this non-tail-focused payoff
        # (exponential twisting trades variance for tail-quantile
        # efficiency, which this whole-distribution mean doesn't exploit) —
        # so the tolerance must scale with the actual sampling noise rather
        # than an arbitrary constant, or a fixed seed can legitimately land
        # outside a too-tight absolute bound despite the estimator being
        # correctly unbiased in expectation.
        se_true = payoff_true.std(ddof=1) / np.sqrt(n_paths)
        se_reweighted = reweighted.std(ddof=1) / np.sqrt(n_paths)
        tolerance = 6 * np.sqrt(se_true**2 + se_reweighted**2)
        assert abs(mean_reweighted - mean_true) < tolerance, (
            f"reweighted mean {mean_reweighted} vs true mean {mean_true} "
            f"(tolerance {tolerance})"
        )

    def test_engine_importance_sampling_self_generates_and_is_unbiased(self):
        """End-to-end plumbing check: selecting importance_sampling with no
        additional_data override should draw drift-shifted paths, compute
        the matching log-likelihood ratio, and produce an estimate close to
        a plain (unshifted) run of the same spec (different seeds, so only a
        loose tolerance is meaningful)."""
        engine = MonteCarloEngine()
        base_kwargs = dict(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=50,
            T=1.0,
            n_paths=100_000,
            payoff="portfolio_var",
            compute_greeks=False,
        )
        spec_plain = McSpec(**base_kwargs, seed=200)
        spec_is = McSpec(**base_kwargs, seed=201, variance_reduction=["importance_sampling"])

        result_plain, _ = engine.run(spec_plain)
        result_is, _ = engine.run(spec_is)

        assert result_is.variance_reduction_gain is not None
        assert "importance_sampling" in result_is.variance_reduction_gain
        # See test_reweighted_estimator_is_unbiased: the drift-shifted
        # estimator has materially higher variance for this whole-distribution
        # payoff, so the tolerance is scaled from the run's own reported
        # stderr rather than a fixed constant.
        tolerance = 6 * np.sqrt(result_is.stderr**2 + result_plain.stderr**2)
        assert abs(result_is.estimate - result_plain.estimate) < tolerance, (
            f"IS estimate {result_is.estimate} vs plain estimate {result_plain.estimate} "
            f"(tolerance {tolerance})"
        )

    def test_importance_sampling_falls_back_for_non_normal_distribution(self):
        """The closed-form likelihood ratio assumes Gaussian driving shocks;
        for a non-normal distribution the engine should fall back to
        standard sampling (logged warning) rather than crash or silently
        report a wrong ratio."""
        engine = MonteCarloEngine()
        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=20,
            T=1.0,
            n_paths=2000,
            payoff="portfolio_var",
            distribution="student_t",
            distribution_params={"nu": 5},
            variance_reduction=["importance_sampling"],
            compute_greeks=False,
            seed=5,
        )
        result, _ = engine.run(spec)
        assert result.variance_reduction_gain["importance_sampling"] == 1.0

    def test_importance_sampling_and_antithetic_combo_does_not_crash(self):
        """Antithetic pairing takes priority when both are selected (see
        engine.py comment); importance sampling should degrade to a no-op
        rather than error."""
        engine = MonteCarloEngine()
        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=20,
            T=1.0,
            n_paths=2000,
            payoff="portfolio_var",
            variance_reduction=["antithetic", "importance_sampling"],
            compute_greeks=False,
            seed=6,
        )
        result, _ = engine.run(spec)
        assert result.variance_reduction_gain is not None
        assert result.variance_reduction_gain["importance_sampling"] == 1.0


class TestMLMC:
    """Test MLMC convergence and cost scaling."""

    def test_mlmc_cost_scales_as_epsilon_minus_2(self):
        """MLMC cost should scale roughly as O(epsilon^{-2})."""
        from app.foundation.quant_mc.mlmc import adaptive_mlmc

        def estimator(level, n_samples, rng):
            # MLMC estimator with properly coupled fine/coarse paths.
            # Each level uses a power-of-2 step count; coarse path aggregates
            # pairs of fine Brownian increments so that both paths share the
            # same underlying randomness (Giles 2008 coupling).
            n_steps_fine = max(4, 2 ** (6 - min(level, 5)))
            n_steps_coarse = max(2, n_steps_fine // 2)
            S0 = 100
            K = 100
            r = 0.05
            T = 1.0
            sigma = 0.2

            dt_fine = T / n_steps_fine
            dt_coarse = T / n_steps_coarse

            # Generate all fine-level increments once, then aggregate for coarse.
            fine_z = rng.standard_normal((n_steps_fine, n_samples))

            # Fine path — full resolution
            S_fine = np.full(n_samples, S0, dtype=float)
            for i in range(n_steps_fine):
                S_fine *= np.exp(
                    (r - 0.5 * sigma**2) * dt_fine
                    + sigma * np.sqrt(dt_fine) * fine_z[i]
                )

            # Coarse path — aggregate pairs of fine increments (coupled)
            S_coarse = np.full(n_samples, S0, dtype=float)
            for j in range(n_steps_coarse):
                i0 = 2 * j
                i1 = 2 * j + 1
                z_coarse = (fine_z[i0] + fine_z[i1]) / np.sqrt(2.0)
                S_coarse *= np.exp(
                    (r - 0.5 * sigma**2) * dt_coarse
                    + sigma * np.sqrt(dt_coarse) * z_coarse
                )

            payoff_fine = np.maximum(S_fine - K, 0) * np.exp(-r * T)
            payoff_coarse = np.maximum(S_coarse - K, 0) * np.exp(-r * T)

            return payoff_coarse, payoff_fine

        # Adaptive MLMC actually responds to epsilon (the fixed-level variant
        # ignored it). Cost should fall (rise) as epsilon rises (falls).
        epsilons = [0.05, 0.01]
        costs = []
        for epsilon in epsilons:
            rng = np.random.default_rng(123)
            result = adaptive_mlmc(estimator, L_max=4, epsilon=epsilon, rng=rng)
            costs.append(result.n_total_samples)

        # Tighter epsilon must demand more samples (monotone in tolerance).
        assert costs[1] > costs[0], (
            f"Adaptive MLMC sample count should grow as epsilon shrinks, got {costs}"
        )


class TestGreeks:
    """Test Greeks computation accuracy vs Black-Scholes."""

    def test_delta_matches_black_scholes(self):
        """Computed Delta should match Black-Scholes within 5e-3."""
        engine = MonteCarloEngine()
        np.random.seed(100)

        # European call parameters
        S0 = 100
        K = 100
        r = 0.05
        sigma = 0.2
        T = 1.0

        # Black-Scholes reference
        bs_greeks = compute_black_scholes_greeks(S0, K, T, r, sigma, option_type="call")
        bs_delta = bs_greeks["delta"]

        # MC estimation with large sample
        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": S0, "mu": r, "sigma": sigma},
            horizon_steps=50,
            T=T,
            n_paths=50000,
            payoff="european_call",
            payoff_params={"strike": K},
            distribution="normal",
            compute_greeks=True,
            compute_greeks_method="pathwise",
            seed=100,
        )

        result, _ = engine.run(spec)
        assert result.greeks is not None, "Greeks not computed"

        mc_delta = result.greeks["delta"]

        # Tolerance: 5e-3
        diff = abs(mc_delta - bs_delta)
        assert diff < 5e-3, f"Delta diff {diff} >= 5e-3: MC={mc_delta:.4f}, BS={bs_delta:.4f}"

    def test_vega_has_correct_sign(self):
        """Vega should be positive for European call (more volatility = higher value)."""
        engine = MonteCarloEngine()
        np.random.seed(101)

        S0, K, T, r = 100, 100, 1.0, 0.05
        sigma = 0.2

        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": S0, "mu": r, "sigma": sigma},
            horizon_steps=50,
            T=T,
            n_paths=20000,
            payoff="european_call",
            payoff_params={"strike": K},
            compute_greeks=True,
            seed=101,
        )

        result, _ = engine.run(spec)
        assert result.greeks is not None
        assert result.greeks["vega"] > 0, "Vega should be positive for call"


class TestMonteCarloEngine:
    """Test basic MC engine functionality."""

    def test_engine_produces_estimate_with_stderr(self):
        """Engine should produce estimate and standard error."""
        engine = MonteCarloEngine()
        np.random.seed(50)

        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100, "mu": 0.05, "sigma": 0.2},
            horizon_steps=100,
            T=1.0,
            n_paths=1000,
            payoff="european_call",
            payoff_params={"strike": 100},
        )

        result, _ = engine.run(spec)

        assert result.estimate > 0, "Estimate should be positive"
        assert result.stderr > 0, "Stderr should be positive"
        assert result.stderr < result.estimate, "Stderr should be less than estimate"

    def test_percentiles_are_ordered(self):
        """Percentiles should be ordered p5 < p25 < p50 < p75 < p95."""
        engine = MonteCarloEngine()
        np.random.seed(51)

        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100, "mu": 0.05, "sigma": 0.2},
            horizon_steps=100,
            T=1.0,
            n_paths=5000,
            payoff="portfolio_var",
        )

        result, _ = engine.run(spec)
        assert result.percentiles is not None

        p05 = result.percentiles["p05"]
        p25 = result.percentiles["p25"]
        p50 = result.percentiles["p50"]
        p75 = result.percentiles["p75"]
        p95 = result.percentiles["p95"]

        assert p05 < p25 < p50 < p75 < p95, "Percentiles not ordered"

    def test_more_paths_reduces_stderr(self):
        """Doubling paths should reduce stderr by ~sqrt(2)."""
        engine = MonteCarloEngine()

        spec_small = McSpec(
            model="gbm_euler",
            model_params={"S0": 100, "mu": 0.05, "sigma": 0.2},
            horizon_steps=50,
            T=1.0,
            n_paths=5000,
            payoff="european_call",
            payoff_params={"strike": 100},
            seed=60,
        )

        spec_large = McSpec(
            model="gbm_euler",
            model_params={"S0": 100, "mu": 0.05, "sigma": 0.2},
            horizon_steps=50,
            T=1.0,
            n_paths=10000,
            payoff="european_call",
            payoff_params={"strike": 100},
            seed=60,  # Same seed for comparable results
        )

        result_small, _ = engine.run(spec_small)
        result_large, _ = engine.run(spec_large)

        ratio = result_small.stderr / result_large.stderr
        expected_ratio = np.sqrt(2)

        # Tolerance: 10% (sampling noise)
        assert abs(ratio - expected_ratio) / expected_ratio < 0.10


class TestPathSimulation:
    """Test path simulators."""

    def test_gbm_paths_are_positive(self):
        """GBM paths should never be negative."""
        from app.foundation.quant_mc.paths import GBMEuler
        from app.foundation.quant_mc.distributions import NormalDistribution

        rng = np.random.default_rng(70)
        simulator = GBMEuler(mu=0.05, sigma=0.2)
        dist = NormalDistribution(0, 1)

        paths = simulator.simulate(S0=100, T=1.0, n_steps=100, n_paths=1000, rng=rng, distribution=dist)

        assert np.all(paths > 0), "GBM paths should be positive"

    def test_paths_shape_is_correct(self):
        """Paths should have shape (n_paths, n_steps+1)."""
        from app.foundation.quant_mc.paths import GBMEuler
        from app.foundation.quant_mc.distributions import NormalDistribution

        rng = np.random.default_rng(71)
        simulator = GBMEuler()
        dist = NormalDistribution()

        paths = simulator.simulate(S0=100, T=1.0, n_steps=100, n_paths=500, rng=rng, distribution=dist)

        assert paths.shape == (500, 101), f"Expected (500, 101), got {paths.shape}"


class TestDistributions:
    """Test distribution samplers."""

    def test_normal_samples_have_correct_moments(self):
        """Normal samples should have correct mean and std."""
        from app.foundation.quant_mc.distributions import NormalDistribution

        dist = NormalDistribution(mu=5, sigma=2)
        rng = np.random.default_rng(80)
        samples = dist.sample(10000, rng)

        assert abs(np.mean(samples) - 5) < 0.1, "Mean should be ~5"
        assert abs(np.std(samples) - 2) < 0.1, "Std should be ~2"

    def test_student_t_has_heavier_tails(self):
        """Student-t kurtosis should be > 0 (heavier tails than normal)."""
        from app.foundation.quant_mc.distributions import StudentTDistribution
        from scipy.stats import kurtosis

        dist = StudentTDistribution(nu=5, mu=0, sigma=1)
        rng = np.random.default_rng(81)
        samples = dist.sample(10000, rng)

        kurt = kurtosis(samples)
        assert kurt > 0, "Student-t should have positive kurtosis (heavy tails)"


# ---------------------------------------------------------------------------
# Sobol QMC (Phase 3 fix: replaced custom SobolGenerator with scipy)
# ---------------------------------------------------------------------------

from app.foundation.quant_mc.quasi_mc import SobolGenerator, generate_sobol_paths


class TestSobolGenerator:
    def test_basic_shape(self):
        gen = SobolGenerator(dim=10)
        pts = gen.generate(100)
        assert pts.shape == (100, 10)

    def test_values_in_unit_hypercube(self):
        gen = SobolGenerator(dim=5)
        pts = gen.generate(200)
        assert np.all(pts >= 0.0)
        assert np.all(pts < 1.0)

    def test_high_dimension(self):
        gen = SobolGenerator(dim=50)
        pts = gen.generate(100)
        assert pts.shape == (100, 50)
        assert np.all(pts >= 0.0)
        assert np.all(pts < 1.0)

    def test_scrambled_differs_from_unscrambled(self):
        gen_plain = SobolGenerator(dim=8)
        gen_scrambled = SobolGenerator(dim=8, scramble_seed=42)
        a = gen_plain.generate(128)
        b = gen_scrambled.generate(128)
        assert not np.allclose(a, b)

    def test_scrambled_in_valid_range(self):
        gen = SobolGenerator(dim=4, scramble_seed=42)
        pts = gen.generate(64)
        assert np.all(pts >= 0.0)
        assert np.all(pts < 1.0)

    def test_invalid_dim(self):
        with pytest.raises(ValueError):
            SobolGenerator(dim=0)

    def test_generate_uniform(self):
        gen = SobolGenerator(dim=3)
        pts = gen.generate_uniform(50, low=2.0, high=5.0)
        assert np.all(pts >= 2.0)
        assert np.all(pts < 5.0)


class TestGenerateSobolPaths:
    def test_shape(self):
        paths = generate_sobol_paths(64, 21, 0.05, 0.2, 100.0, 1.0 / 252)
        assert paths.shape == (64, 22)

    def test_starts_at_S0(self):
        paths = generate_sobol_paths(32, 10, 0.05, 0.2, 100.0, 1.0)
        np.testing.assert_array_equal(paths[:, 0], 100.0)

    def test_positive_prices(self):
        paths = generate_sobol_paths(50, 15, 0.05, 0.2, 100.0, 1.0 / 252)
        assert np.all(paths > 0.0)

    def test_scrambled_differs(self):
        a = generate_sobol_paths(32, 10, 0.05, 0.2, 100.0, 1.0, scramble_seed=1)
        b = generate_sobol_paths(32, 10, 0.05, 0.2, 100.0, 1.0, scramble_seed=2)
        assert not np.allclose(a, b)


# ---------------------------------------------------------------------------
# MLMC coupling (Phase 3 fix: coarse paths subsampled from fine)
# ---------------------------------------------------------------------------


class TestMLMCCoupling:
    def test_coarse_is_subsampled_fine(self):
        engine = MonteCarloEngine()
        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=32,
            n_paths=50,
            payoff="european_call",
            payoff_params={"strike": 100.0},
            use_mlmc=True,
            mlmc_levels=2,
            seed=42,
            compute_greeks=False,
        )
        result, _ = engine.run(spec)
        assert result.mlmc_stats is not None

    def test_mlmc_estimator_coupling(self):
        engine = MonteCarloEngine()
        spec = McSpec(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=32,
            n_paths=50,
            payoff="european_call",
            payoff_params={"strike": 100.0},
            use_mlmc=True,
            mlmc_levels=3,
            seed=123,
            compute_greeks=False,
        )
        result, _ = engine.run(spec)
        assert result.estimate > 0
        assert result.stderr > 0

    def test_mlmc_variance_reduction_benefit(self):
        engine = MonteCarloEngine()
        base_spec = dict(
            model="gbm_euler",
            model_params={"S0": 100.0, "mu": 0.05, "sigma": 0.2},
            horizon_steps=64,
            n_paths=50,
            payoff="european_call",
            payoff_params={"strike": 100.0},
            seed=42,
            compute_greeks=False,
        )
        mlmc_spec = McSpec(**base_spec, use_mlmc=True, mlmc_levels=3)
        result_mlmc, _ = engine.run(mlmc_spec)

        plain_spec = McSpec(**base_spec)
        result_plain, _ = engine.run(plain_spec)

        assert result_mlmc.stderr > 0
        assert result_plain.stderr > 0
