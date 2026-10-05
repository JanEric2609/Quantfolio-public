"""Monte Carlo engine facade - the main entry point for MC simulations.

Orchestrates: path simulation + variance reduction + Greeks + storage.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

import numpy as np
from scipy.stats import norm

from .distributions import create_distribution
from .greeks import compute_greeks_likelihood_ratio, compute_greeks_pathwise
from .mlmc import fixed_level_mlmc
from .paths import GBMEuler, create_simulator
from .payoffs import create_payoff
from .storage import compute_path_summary, downsample_paths
from .variance_reduction import apply_variance_reduction

logger = logging.getLogger(__name__)


@dataclass
class McSpec:
    """Specification for a Monte Carlo run."""

    # Path simulation
    model: Literal["gbm_euler", "gbm_milstein", "heston_euler", "ou_euler"] = "gbm_euler"
    model_params: dict = field(default_factory=dict)

    # Time discretization
    horizon_steps: int = 100
    T: float = 1.0

    # Sample count
    n_paths: int = 10000

    # Payoff
    payoff: Literal[
        "european_call",
        "european_put",
        "asian_call",
        "lookback_call",
        "barrier_call",
        "portfolio_var",
        "portfolio_es",
    ] = "european_call"
    payoff_params: dict = field(default_factory=dict)

    # Distribution
    distribution: Literal["normal", "student_t", "nig"] = "normal"
    distribution_params: dict = field(default_factory=dict)

    # Variance reduction
    variance_reduction: list[str] = field(default_factory=list)  # ["antithetic", "control_variate", ...]

    # Optional caller-supplied data for the non-antithetic reducers
    # (control_values, log_likelihood_ratio, stratum_indices). None means
    # "no override" — the engine self-generates what it can (see
    # MonteCarloEngine._build_variance_reduction_data) rather than treating
    # an absent value as "nothing was requested" (deepdive-03-quantlab
    # Issue 2: this field used to not exist at all, so a caller-supplied
    # override was silently dropped even before reaching the engine).
    additional_data: Optional[dict] = None

    # MLMC
    use_mlmc: bool = False
    mlmc_levels: Optional[int] = None

    # Options
    seed: Optional[int] = None
    return_paths: bool = False
    compute_greeks: bool = True
    compute_greeks_method: Literal["pathwise", "likelihood_ratio"] = "pathwise"

    def __post_init__(self):
        if self.model_params is None:
            self.model_params = {}
        if self.payoff_params is None:
            self.payoff_params = {}
        if self.distribution_params is None:
            self.distribution_params = {}
        if self.variance_reduction is None:
            self.variance_reduction = []

    def to_json(self) -> str:
        spec_dict = asdict(self)
        spec_dict["horizon_steps"] = int(self.horizon_steps)
        spec_dict["n_paths"] = int(self.n_paths)
        return json.dumps(spec_dict, default=str)


@dataclass
class McResult:
    """Result of a Monte Carlo run."""

    estimate: float
    stderr: float
    n_effective: Optional[int]
    variance_reduction_gain: Optional[dict] = None
    greeks: Optional[dict] = None
    percentiles: Optional[dict] = None
    paths_summary: Optional[dict] = None
    mlmc_stats: Optional[dict] = None

    def to_json(self) -> str:
        result_dict = asdict(self)
        return json.dumps(result_dict, default=str)


class MonteCarloEngine:
    """Main MC engine orchestrator."""

    def run(self, spec: McSpec) -> tuple[McResult, Optional[np.ndarray]]:
        """Execute a Monte Carlo simulation.

        Returns:
            (result, paths) where paths is downsampled if return_paths=True else None.
        """
        # Setup RNG with seed control
        if spec.seed is not None:
            rng = np.random.default_rng(spec.seed)
        else:
            rng = np.random.default_rng()

        # MLMC path if requested
        if spec.use_mlmc:
            return self._run_mlmc(spec, rng)

        # Standard MC path
        use_antithetic = "antithetic" in spec.variance_reduction
        # Antithetic pairing already draws its own matched +Z/-Z legs, which
        # is incompatible with also drift-shifting the sampling distribution
        # for importance sampling in the same pass — antithetic takes
        # priority (matching the pre-existing precedence of this branch),
        # and importance sampling degrades to a logged no-op below.
        use_importance_sampling = "importance_sampling" in spec.variance_reduction and not use_antithetic

        antithetic_leg_payoffs: np.ndarray | None = None
        importance_sampling_log_lr: np.ndarray | None = None

        if use_antithetic:
            # True antithetic pairing requires the same Z draws for the two
            # legs (one with +Z, one with -Z). Generate both in a single call
            # so the reducer's variance-ratio analysis is computed against the
            # *matched* leg rather than an independent fresh sample.
            payoff_values, antithetic_leg_payoffs, paths = self._simulate_antithetic_pair(spec, rng)
        elif use_importance_sampling:
            try:
                payoff_values, paths, importance_sampling_log_lr = (
                    self._simulate_paths_importance_sampling(spec, rng)
                )
            except ValueError as exc:
                logger.warning(
                    "Importance sampling drift-shift unavailable (%s); "
                    "falling back to standard sampling.",
                    exc,
                )
                payoff_values, paths = self._simulate_paths(spec, rng, antithetic=False)
        else:
            payoff_values, paths = self._simulate_paths(spec, rng, antithetic=False)

        # Variance reduction
        vr_gains = None
        if spec.variance_reduction:
            additional_data = self._build_variance_reduction_data(
                spec,
                paths,
                antithetic_leg_payoffs=antithetic_leg_payoffs,
                importance_sampling_log_lr=importance_sampling_log_lr,
            )

            payoff_values, vr_gains = apply_variance_reduction(
                payoff_values,
                spec.variance_reduction,
                additional_data,
            )

        # Statistics
        estimate = np.mean(payoff_values)
        stderr = np.std(payoff_values) / np.sqrt(len(payoff_values))
        n_effective = len(payoff_values)

        # Greeks
        greeks_dict = None
        if spec.compute_greeks:
            greeks_dict = self._compute_greeks(spec, payoff_values, paths, rng)

        # Percentiles
        percentiles = {
            "p05": float(np.percentile(payoff_values, 5)),
            "p25": float(np.percentile(payoff_values, 25)),
            "p50": float(np.percentile(payoff_values, 50)),
            "p75": float(np.percentile(payoff_values, 75)),
            "p95": float(np.percentile(payoff_values, 95)),
        }

        # Path summary
        paths_summary = compute_path_summary(paths) if paths is not None else None

        result = McResult(
            estimate=float(estimate),
            stderr=float(stderr),
            n_effective=int(n_effective),
            variance_reduction_gain=vr_gains,
            greeks=greeks_dict,
            percentiles=percentiles,
            paths_summary=paths_summary,
        )

        # Return downsampled paths if requested
        downsampled = None
        if spec.return_paths and paths is not None:
            downsampled = downsample_paths(paths)

        return result, downsampled

    def _simulate_paths(
        self,
        spec: McSpec,
        rng: np.random.Generator,
        *,
        antithetic: bool = False,
        antithetic_leg_only: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Simulate paths and compute payoffs.

        Args:
            antithetic: When True, generate n_paths//2 path pairs using +Z and -Z.
                        The returned payoffs are the average of the two legs, which
                        halves variance for symmetric payoffs.
            antithetic_leg_only: When True (and antithetic=True), return only the -Z
                        leg payoffs (used by the VR call-site to compute the variance
                        reduction gain ratio).
        """
        dist = create_distribution(spec.distribution, spec.distribution_params)
        simulator = create_simulator(spec.model, spec.model_params)
        payoff_func = create_payoff(spec.payoff, spec.payoff_params)

        S0 = spec.model_params.get("S0", 100.0)

        if antithetic:
            payoffs_pos, payoffs_neg, paths_pos = self._draw_antithetic_legs(
                spec, rng, simulator, payoff_func, dist, S0
            )
            if antithetic_leg_only:
                return payoffs_neg, paths_pos
            # Average the paired payoffs — this is the antithetic estimator.
            return (payoffs_pos + payoffs_neg) / 2.0, paths_pos

        # Standard (non-antithetic) path
        paths = simulator.simulate(S0, spec.T, spec.horizon_steps, spec.n_paths, rng, dist)
        payoff_values = payoff_func.evaluate(paths)
        return payoff_values, paths

    def _simulate_antithetic_pair(
        self,
        spec: McSpec,
        rng: np.random.Generator,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Simulate true antithetic pairs in a single call.

        Returns (paired_payoffs, antithetic_leg_payoffs, paths_pos).
        - paired_payoffs is the averaged estimator
          (payoffs_pos + payoffs_neg) / 2.
        - antithetic_leg_payoffs is the raw -Z leg, preserved so the variance-
          reduction analysis can compute the correct variance ratio against
          *the same* underlying draws.
        """
        dist = create_distribution(spec.distribution, spec.distribution_params)
        simulator = create_simulator(spec.model, spec.model_params)
        payoff_func = create_payoff(spec.payoff, spec.payoff_params)
        S0 = spec.model_params.get("S0", 100.0)

        payoffs_pos, payoffs_neg, paths_pos = self._draw_antithetic_legs(
            spec, rng, simulator, payoff_func, dist, S0
        )
        paired = (payoffs_pos + payoffs_neg) / 2.0
        return paired, payoffs_neg, paths_pos

    @staticmethod
    def _draw_antithetic_legs(
        spec: McSpec,
        rng: np.random.Generator,
        simulator,
        payoff_func,
        dist,
        S0: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Draw matched +Z and -Z legs using the same underlying RNG state.

        Snapshots the RNG state before the +Z leg and restores it before the
        -Z leg so the second leg consumes the *same* Z draws (then negates
        them). Without this, the second leg would draw a fresh independent
        batch and antithetic pairing would degenerate to two independent
        simulations.
        """
        n_half = max(spec.n_paths // 2, 1)

        rng_state = rng.bit_generator.state

        paths_pos = simulator.simulate(S0, spec.T, spec.horizon_steps, n_half, rng, dist)
        payoffs_pos = payoff_func.evaluate(paths_pos)

        # Reset the RNG so the negated distribution draws the same Z values.
        rng.bit_generator.state = rng_state

        class _NegatedDist:
            """Wraps a distribution and negates every sample (true antithetic)."""

            def __init__(self, inner):
                self._inner = inner

            def sample(self, n, rng_inner):
                return -self._inner.sample(n, rng_inner)

        neg_dist = _NegatedDist(dist)
        paths_neg = simulator.simulate(S0, spec.T, spec.horizon_steps, n_half, rng, neg_dist)
        payoffs_neg = payoff_func.evaluate(paths_neg)

        return payoffs_pos, payoffs_neg, paths_pos

    def _simulate_paths_importance_sampling(
        self,
        spec: McSpec,
        rng: np.random.Generator,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Draw paths under an exponentially-tilted (drift-shifted) GBM measure Q,
        and compute the matching per-path log-likelihood ratio log(dP/dQ)
        needed to reweight the Q-sampled payoffs back into an unbiased
        estimator under the true measure P.

        This is real importance sampling (Glasserman, Heidelberger &
        Shahabuddin 2000, "Variance Reduction Techniques for Estimating
        Value-at-Risk") — not just reweighting already-unbiased draws: the
        *sampling distribution itself* is shifted here, via a new GBMEuler
        instance parameterised with a shifted drift `mu + theta`. `GBMEuler`
        already accepts an arbitrary `mu`, so no change to paths.py is
        required to draw from the shifted measure.

        Shift sizing ("exponential twisting to the target quantile", GHS
        2000 §3): theta is chosen so the shifted distribution's *mean*
        terminal log-return lands on the BASE (unshifted) distribution's
        analytic 5th-percentile terminal log-return — i.e. Q concentrates
        its mass where P's left tail (VaR-relevant losses) lives. This is
        available in closed form for GBM without a pilot simulation:
            a  = (mu - sigma^2/2) * T            # base mean of ln(S_T/S0)
            s  = sigma * sqrt(T)                 # base std of ln(S_T/S0)
            target = a + s * Phi^-1(0.05)         # base P5 quantile of ln(S_T/S0)
            theta  = (target - a) / T = sigma * Phi^-1(0.05) / sqrt(T)
        Phi^-1(0.05) ~= -1.6449, so theta < 0 (drift is pushed down,
        oversampling loss scenarios), matching the "theta<0 to oversample
        the left tail" heuristic.

        Likelihood ratio derivation (verified against this codebase's exact
        GBM closed form S(t) = S0*exp((mu-sigma^2/2)t + sigma*W(t)), paths.py
        docstring): under P, x := ln(S_T/S0) ~ N(a, sigma^2*T); under Q
        (drift shifted by theta), x ~ N(a + theta*T, sigma^2*T) — same
        variance, shifted mean only, because GBMEuler draws the SAME
        standard-normal shocks Z_i and only the deterministic drift term
        changes. The ratio of two Gaussian densities with equal variance and
        means differing by theta*T, evaluated at a Q-sampled x = (a+theta*T)
        + sigma*W_T (W_T the realised driving Brownian terminal value,
        algebraically recovered below from the actually-simulated shifted
        path), simplifies to:
            log(dP/dQ) = -theta * W_T / sigma - theta^2 * T / (2 * sigma^2)
        Confirmed both symbolically (sympy, expanding the two log-densities)
        and numerically (a fixed-seed 2M-path check that
        mean(payoff_shifted * exp(log_llr)) ~= mean(payoff_unshifted)); see
        tests/services/test_quant_mc.py::TestImportanceSampling.

        Only implemented for model="gbm_euler" with distribution="normal" —
        the density-ratio algebra above assumes Gaussian driving shocks. Any
        other combination raises ValueError so the caller (MonteCarloEngine.run)
        can fall back to standard sampling with a logged warning rather than
        silently reporting a wrong likelihood ratio.
        """
        if spec.model != "gbm_euler":
            raise ValueError(
                f"importance sampling drift-shift is only implemented for "
                f"model='gbm_euler', got model={spec.model!r}"
            )
        if spec.distribution != "normal":
            raise ValueError(
                f"importance sampling drift-shift is only implemented for "
                f"distribution='normal' (Gaussian shocks), got "
                f"distribution={spec.distribution!r}"
            )

        dist = create_distribution(spec.distribution, spec.distribution_params)
        payoff_func = create_payoff(spec.payoff, spec.payoff_params)

        S0 = spec.model_params.get("S0", 100.0)
        mu = spec.model_params.get("mu", 0.05)
        sigma = spec.model_params.get("sigma", 0.2)
        T = spec.T

        if sigma <= 0 or T <= 0:
            raise ValueError(
                f"importance sampling drift-shift requires sigma>0 and T>0, "
                f"got sigma={sigma}, T={T}"
            )

        z_p05 = float(norm.ppf(0.05))  # ~ -1.6449
        theta = sigma * z_p05 / np.sqrt(T)

        shifted_simulator = GBMEuler(mu=mu + theta, sigma=sigma)
        paths = shifted_simulator.simulate(
            S0, T, spec.horizon_steps, spec.n_paths, rng, dist
        )
        payoff_values = payoff_func.evaluate(paths)

        # Recover the realised driving Brownian terminal value from the
        # SHIFTED path, inverting the closed form with the shifted drift
        # actually used to build it: ln(S_T/S0) = (mu+theta-sigma^2/2)*T + sigma*W_T
        log_return = np.log(paths[:, -1] / S0)
        W_T = (log_return - (mu + theta - 0.5 * sigma**2) * T) / sigma

        log_likelihood_ratio = -theta * W_T / sigma - theta**2 * T / (2 * sigma**2)

        return payoff_values, paths, log_likelihood_ratio

    @staticmethod
    def _stratify_terminal_prices(paths: np.ndarray, n_strata: int = 10) -> np.ndarray:
        """Bin terminal prices into n_strata empirical-quantile buckets.

        Post-hoc stratification of already-drawn i.i.d. paths (Owen ch. 8),
        matching StratifiedReducer's post-stratification-by-index math — this
        only supplies the missing `stratum_indices`, it doesn't change how
        the reducer averages within each stratum.
        """
        terminal = paths[:, -1]
        edges = np.quantile(terminal, np.linspace(0.0, 1.0, n_strata + 1))
        # Interior edges only; searchsorted assigns each value to bucket
        # [edges[i], edges[i+1]) except the last bucket, which is closed.
        indices = np.searchsorted(edges[1:-1], terminal, side="right")
        return np.clip(indices, 0, n_strata - 1).astype(int)

    def _build_variance_reduction_data(
        self,
        spec: McSpec,
        paths: np.ndarray,
        *,
        antithetic_leg_payoffs: np.ndarray | None,
        importance_sampling_log_lr: np.ndarray | None,
    ) -> dict:
        """Assemble additional_data for apply_variance_reduction.

        Starts from any caller-supplied override (spec.additional_data) and
        self-generates whatever is still missing for the selected
        techniques, using only quantities already available from the main
        simulation pass — mirroring how antithetic already self-generates
        antithetic_payoffs. An explicit override always wins (never
        clobbered by self-generation).
        """
        additional_data: dict = dict(spec.additional_data or {})

        if antithetic_leg_payoffs is not None:
            additional_data.setdefault("antithetic_payoffs", antithetic_leg_payoffs)

        if importance_sampling_log_lr is not None:
            additional_data.setdefault("log_likelihood_ratio", importance_sampling_log_lr)

        if "control_variate" in spec.variance_reduction and "control_values" not in additional_data:
            # Textbook Black-Scholes-style control variate (Glasserman ch. 4):
            # the terminal price itself. CAVEAT (deepdive-03-quantlab Issue 2):
            # for the portfolio_var/portfolio_es payoffs this Scenarios view
            # always submits, the payoff is an EXACT affine function of S_T
            # ((S_T/S0 - 1) * 10000), so rho=1 and the reported
            # variance-reduction ratio will look artificially perfect
            # (adjusted variance -> ~0, ratio -> very large). This is
            # mathematically valid, not a bug — the control variate math
            # itself (ControlVariateReducer.reduce) is unchanged and
            # already sample-mean-centered — flagging here so a future
            # reader isn't confused by a suspiciously perfect result.
            additional_data["control_values"] = paths[:, -1]

        if "stratified" in spec.variance_reduction and "stratum_indices" not in additional_data:
            additional_data["stratum_indices"] = self._stratify_terminal_prices(paths)

        required_key_by_technique = {
            "control_variate": "control_values",
            "importance_sampling": "log_likelihood_ratio",
            "stratified": "stratum_indices",
        }
        for technique in spec.variance_reduction:
            required_key = required_key_by_technique.get(technique)
            if required_key and required_key not in additional_data:
                logger.warning(
                    "Variance reduction technique '%s' requires "
                    "additional_data['%s'], which could not be supplied or "
                    "self-generated for this run (model=%s, distribution=%s) "
                    "— skipping.",
                    technique,
                    required_key,
                    spec.model,
                    spec.distribution,
                )

        return additional_data

    def _compute_greeks(
        self,
        spec: McSpec,
        payoff_values: np.ndarray,
        paths: np.ndarray,
        rng: np.random.Generator,
    ) -> dict:
        """Compute Greeks using the specified method."""
        S0 = spec.model_params.get("S0", 100.0)
        sigma = spec.model_params.get("sigma", 0.2)

        if spec.compute_greeks_method == "pathwise":
            K = spec.payoff_params.get("strike") or spec.payoff_params.get("K")
            r = spec.model_params.get("r", spec.model_params.get("mu", 0.05))
            T_val = spec.T
            greeks = compute_greeks_pathwise(
                S0,
                payoff_values,
                paths,
                np.zeros((len(payoff_values), paths.shape[1])),
                K=K,
                r=r,
                sigma=sigma,
                T=T_val,
            )
        else:  # likelihood_ratio
            greeks = compute_greeks_likelihood_ratio(
                S0,
                payoff_values,
                paths,
                sigma,
                spec.T,
            )

        return greeks.to_dict()

    def _run_mlmc(self, spec: McSpec, rng: np.random.Generator) -> tuple[McResult, None]:
        """Execute MLMC algorithm.

        Coarse and fine paths are COUPLED: fine paths are generated first,
        then coarse paths are created by subsampling the fine paths.  This
        ensures the difference (fine - coarse) has small variance, which is
        the key to MLMC variance reduction.
        """

        # Use adaptive or fixed MLMC
        L = spec.mlmc_levels or 4

        def estimator(level: int, n_samples: int, rng_inner):
            """Estimator function for MLMC."""
            dist = create_distribution(spec.distribution, spec.distribution_params)
            simulator = create_simulator(spec.model, spec.model_params)
            payoff_func = create_payoff(spec.payoff, spec.payoff_params)
            S0 = spec.model_params.get("S0", 100.0)

            if level == 0:
                # Coarsest grid for base level
                n_coarse_steps = max(2, spec.horizon_steps // (2**L))
                paths_coarse = simulator.simulate(S0, spec.T, n_coarse_steps, n_samples, rng_inner, dist)
                P_coarse = payoff_func.evaluate(paths_coarse)
                return P_coarse, P_coarse

            n_fine_steps = max(4, spec.horizon_steps // (2**(level - 1)))
            n_coarse_steps = max(2, spec.horizon_steps // (2**level))

            paths_fine = simulator.simulate(S0, spec.T, n_fine_steps, n_samples, rng_inner, dist)

            # Include terminal index for proper coupling
            coarse_indices = np.linspace(0, n_fine_steps, n_coarse_steps + 1, dtype=int)
            coarse_indices = np.clip(coarse_indices, 0, n_fine_steps)
            P_coarse = payoff_func.evaluate(paths_fine[:, coarse_indices])
            P_fine = payoff_func.evaluate(paths_fine)

            return P_coarse, P_fine

        mlmc_result = fixed_level_mlmc(estimator, L, spec.n_paths, rng)

        result = McResult(
            estimate=mlmc_result.estimate,
            stderr=mlmc_result.std_error,
            n_effective=mlmc_result.n_total_samples,
            mlmc_stats={
                "n_levels": mlmc_result.n_levels,
                "n_total_samples": mlmc_result.n_total_samples,
                "level_stats": mlmc_result.level_stats,
            },
        )

        return result, None
