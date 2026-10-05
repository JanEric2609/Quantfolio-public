"""Multilevel Monte Carlo (MLMC) for efficient estimation.

Based on Giles 2008. Computes E[P] = E[P_0] + sum_l E[P_l - P_{l-1}]
using a hierarchy of discretization levels.
"""

from dataclasses import dataclass
from typing import Callable, Tuple, Optional
import numpy as np


@dataclass
class MLMCLevel:
    """A single level in the MLMC hierarchy."""

    level: int
    n_steps: int  # Coarse steps at this level
    n_samples: int  # Number of samples to draw
    payoff_coarse: Optional[np.ndarray] = None  # E[P_l]
    payoff_fine: Optional[np.ndarray] = None  # Coupled fine-level payoff


@dataclass
class MLMCResult:
    """MLMC estimation result."""

    estimate: float
    variance: float
    std_error: float
    n_levels: int
    n_total_samples: int
    level_stats: list[dict]  # Per-level statistics


def adaptive_mlmc(
    estimator: Callable[[int, int, np.random.Generator], Tuple[np.ndarray, np.ndarray]],
    L_max: int = 10,
    epsilon: float = 0.01,
    rng: Optional[np.random.Generator] = None,
) -> MLMCResult:
    """Adaptive MLMC: pick L and N_l to achieve MSE ≈ epsilon² with minimal cost.

    Follows Giles & Szpruch 2012, Algorithm 2.

    Args:
        estimator: Callable(level, n_samples, rng) -> (P_coarse, P_fine)
                   Returns payoffs at coarse and fine levels.
        L_max: Maximum level to consider
        epsilon: Target root-MSE
        rng: Random number generator
    """
    if rng is None:
        rng = np.random.default_rng()

    # Stage 1: Estimate variances and costs at each level
    N_l = np.zeros(L_max + 1, dtype=int)
    V_l = np.zeros(L_max + 1)  # Variance per level
    C_l = np.zeros(L_max + 1)  # Cost per level
    estimates = np.zeros(L_max + 1)

    # Pilot sampling: 50 samples per level
    for lvl in range(L_max + 1):
        P_coarse, P_fine = estimator(lvl, 50, rng)
        estimates[lvl] = np.mean(P_coarse)

        if lvl == 0:
            V_l[lvl] = np.var(P_coarse)
            C_l[lvl] = 1.0  # Normalized cost for level 0
        else:
            diff = P_fine - P_coarse
            V_l[lvl] = np.var(diff)
            C_l[lvl] = 2.0**lvl  # Cost grows exponentially

    # Stage 2: Adaptive level selection (Giles' algorithm).
    # We want the *highest* level whose correction variance is below epsilon²/2,
    # i.e. the correction at that level is already small enough that adding more
    # levels would not materially reduce bias.
    # If no level meets the target, fall back to L_max (finest level tried) so
    # that we never silently collapse to level-0-only sampling.
    target_var = (epsilon**2) / 2
    L = L_max  # default: use all levels if none meet the target
    for lvl in range(L_max, -1, -1):
        if V_l[lvl] < target_var:
            L = lvl
            break
    else:
        # No level met the variance target; use L_max and warn.
        import warnings
        warnings.warn(
            f"adaptive_mlmc: no pilot level achieved variance < {target_var:.3g} "
            f"(epsilon={epsilon}). Using L={L_max} (all levels). "
            "Consider increasing L_max or relaxing epsilon.",
            RuntimeWarning,
            stacklevel=2,
        )

    # Stage 3: Sample allocation via Giles formula
    # N_l = ceil(2 / eps^2 * sqrt(V_l / C_l) * sum_l sqrt(V_l * C_l)) — total
    # sample count scales as O(eps^{-2}), so shrinking the target tolerance
    # increases N_l rather than decreasing it.
    weight_sum = float(np.sum(np.sqrt(V_l[: L + 1] * C_l[: L + 1])))
    scale = (2.0 / max(epsilon**2, 1e-20)) * weight_sum
    for lvl in range(L + 1):
        per_level = np.sqrt(V_l[lvl] / C_l[lvl]) * scale
        N_l[lvl] = max(int(np.ceil(per_level)), 10)

    # Stage 4: Execute sampling
    level_stats = []
    total_estimate = 0.0
    total_variance = 0.0

    for lvl in range(L + 1):
        P_coarse, P_fine = estimator(lvl, N_l[lvl], rng)

        if lvl == 0:
            estimate_l = np.mean(P_coarse)
            var_l = np.var(P_coarse) / N_l[lvl]
        else:
            diff = P_fine - P_coarse
            estimate_l = np.mean(diff)
            var_l = np.var(diff) / N_l[lvl]

        total_estimate += estimate_l
        total_variance += var_l

        level_stats.append({
            "level": lvl,
            "n_samples": N_l[lvl],
            "estimate": estimate_l,
            "variance": var_l,
            "std_error": np.sqrt(var_l),
        })

    return MLMCResult(
        estimate=float(total_estimate),
        variance=float(total_variance),
        std_error=float(np.sqrt(total_variance)),
        n_levels=L + 1,
        n_total_samples=int(np.sum(N_l[:L + 1])),
        level_stats=level_stats,
    )


def fixed_level_mlmc(
    estimator: Callable[[int, int, np.random.Generator], Tuple[np.ndarray, np.ndarray]],
    L: int = 4,
    N_base: int = 100,
    rng: Optional[np.random.Generator] = None,
) -> MLMCResult:
    """MLMC with fixed level L and fixed N_l = N_base * 2^{-(l)} samples.

    Simpler than adaptive MLMC but less optimal.
    """
    if rng is None:
        rng = np.random.default_rng()

    level_stats = []
    total_estimate = 0.0
    total_variance = 0.0

    for lvl in range(L + 1):
        n_samples = int(max(N_base / (2**lvl), 10))
        P_coarse, P_fine = estimator(lvl, n_samples, rng)

        if lvl == 0:
            estimate_l = np.mean(P_coarse)
            var_l = np.var(P_coarse) / n_samples
        else:
            diff = P_fine - P_coarse
            estimate_l = np.mean(diff)
            var_l = np.var(diff) / n_samples

        total_estimate += estimate_l
        total_variance += var_l

        level_stats.append({
            "level": lvl,
            "n_samples": n_samples,
            "estimate": estimate_l,
            "variance": var_l,
            "std_error": np.sqrt(var_l),
        })

    return MLMCResult(
        estimate=float(total_estimate),
        variance=float(total_variance),
        std_error=float(np.sqrt(total_variance)),
        n_levels=L + 1,
        n_total_samples=int(sum(s["n_samples"] for s in level_stats)),
        level_stats=level_stats,
    )
