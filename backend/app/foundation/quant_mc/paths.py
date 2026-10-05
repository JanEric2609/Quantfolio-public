"""Path simulation for stochastic models: GBM, Heston, OU, Merton, CIR."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
import numpy as np
from typing import Literal
from .distributions import Distribution

logger = logging.getLogger(__name__)


@dataclass
class PathSimulator(ABC):
    """Base class for path simulation."""

    @abstractmethod
    def simulate(
        self,
        S0: float,
        T: float,
        n_steps: int,
        n_paths: int,
        rng: np.random.Generator,
        distribution: Distribution,
    ) -> np.ndarray:
        """Simulate paths. Returns (n_paths, n_steps+1) array."""
        pass


@dataclass
class GBMEuler(PathSimulator):
    """Geometric Brownian Motion — exact solution via cumsum.

    dS = μ*S*dt + σ*S*dW

    Draws all shocks at once and builds paths via the closed-form solution
    S(t) = S0 * exp((μ - σ²/2) * t + σ * W(t)), which is exact (no Euler
    discretisation error) and matches the conditional distribution of the SDE.
    The RNG stream differs from the per-step Euler loop (shocks are consumed
    contiguously per time-step rather than interleaved), but the marginal
    distribution at every time slice is identical.
    """

    mu: float = 0.05
    sigma: float = 0.2

    def simulate(
        self,
        S0: float,
        T: float,
        n_steps: int,
        n_paths: int,
        rng: np.random.Generator,
        distribution: Distribution,
    ) -> np.ndarray:
        dt = T / n_steps

        # Draw all shocks at once: original loop draws n_paths per step,
        # so we draw n_paths * n_steps and reshape to (n_steps, n_paths)
        # to preserve per-time-step contiguous consumption order, then transpose.
        all_dW = distribution.sample(n_paths * n_steps, rng).reshape((n_steps, n_paths)).T
        all_dW *= np.sqrt(dt)

        # Closed-form log-returns (exact GBM, no Euler bias)
        drift = (self.mu - 0.5 * self.sigma**2) * dt
        log_returns = drift + self.sigma * all_dW
        cumsum = np.cumsum(log_returns, axis=1)

        paths = np.zeros((n_paths, n_steps + 1))
        paths[:, 0] = S0
        paths[:, 1:] = S0 * np.exp(cumsum)

        return paths


@dataclass
class GBMMilstein(PathSimulator):
    """GBM via Milstein discretization (higher-order).

    Includes the Lévy term for stronger convergence.
    The state recursion is inherently sequential (S_{t} depends on S_{t-1}),
    but all random shocks are pre-drawn to consolidate RNG calls.
    """

    mu: float = 0.05
    sigma: float = 0.2

    def simulate(
        self,
        S0: float,
        T: float,
        n_steps: int,
        n_paths: int,
        rng: np.random.Generator,
        distribution: Distribution,
    ) -> np.ndarray:
        dt = T / n_steps
        paths = np.zeros((n_paths, n_steps + 1))
        paths[:, 0] = S0

        # Pre-draw all shocks — preserves per-step contiguous draw order
        all_dW = distribution.sample(n_paths * n_steps, rng).reshape((n_steps, n_paths)).T
        all_dW *= np.sqrt(dt)

        for t in range(1, n_steps + 1):
            S = paths[:, t - 1]
            dW = all_dW[:, t - 1]

            # Milstein scheme for GBM (RESEARCH_NOTES §5):
            #   S_{n+1} = S_n + μ S_n Δt + σ S_n ΔW + (1/2) σ² S_n (ΔW² - Δt)
            # The correction term (1/2) σ² S_n (ΔW² - Δt) lifts strong order to 1.0.
            paths[:, t] = (
                S
                + self.mu * S * dt
                + self.sigma * S * dW
                + 0.5 * self.sigma**2 * S * (dW**2 - dt)
            )

        return paths


@dataclass
class HestonEuler(PathSimulator):
    """Heston model (stochastic volatility).

    dS = μ*S*dt + sqrt(v)*S*dW_S
    dv = κ*(θ - v)*dt + ξ*sqrt(v)*dW_v
    ρ = correlation between dW_S and dW_v

    The variance recursion is inherently sequential; all random shocks are
    pre-drawn to consolidate RNG calls.
    """

    mu: float = 0.05
    kappa: float = 0.5
    theta: float = 0.04
    xi: float = 0.3
    rho: float = -0.5
    v0: float = 0.04

    def simulate(
        self,
        S0: float,
        T: float,
        n_steps: int,
        n_paths: int,
        rng: np.random.Generator,
        distribution: Distribution,
    ) -> np.ndarray:
        dt = T / n_steps
        paths = np.zeros((n_paths, n_steps + 1))
        vol_paths = np.zeros((n_paths, n_steps + 1))
        paths[:, 0] = S0
        vol_paths[:, 0] = self.v0

        # Feller condition: 2 * kappa * theta > xi^2 ensures variance stays
        # strictly positive. When violated, variance can hit zero causing
        # numerical issues (though full-truncation below prevents NaN).
        feller_lhs = 2 * self.kappa * self.theta
        feller_rhs = self.xi ** 2
        if feller_lhs <= feller_rhs:
            logger.warning(
                "Heston Feller condition violated: 2*kappa*theta (%.4f) <= xi^2 (%.4f). "
                "Variance may hit zero; full-truncation scheme prevents NaN but "
                "distribution may deviate from theoretical Heston model.",
                feller_lhs,
                feller_rhs,
            )

        # Pre-draw all shocks for Z1 and Z2 in interleaved order matching the
        # original per-step loop: for each step we draw Z1 then Z2.
        # RNG stream: [Z1_t0 (n_paths), Z2_t0 (n_paths), Z1_t1 (n_paths), ...]
        all_shocks = distribution.sample(2 * n_paths * n_steps, rng).reshape((2 * n_steps, n_paths))
        all_Z1 = all_shocks[0::2].T  # (n_paths, n_steps)
        all_Z2 = all_shocks[1::2].T  # (n_paths, n_steps)
        sqrt_dt = np.sqrt(dt)

        for t in range(1, n_steps + 1):
            # Build correlated increments through the passed distribution so that
            # heavy-tailed / NIG shocks are honoured (RESEARCH_NOTES §6 + C9 fix).
            # Cholesky decomposition of the 2×2 correlation matrix:
            #   dW_S = Z1 * sqrt(dt)
            #   dW_v = (rho*Z1 + sqrt(1-rho^2)*Z2) * sqrt(dt)
            Z1 = all_Z1[:, t - 1]
            Z2 = all_Z2[:, t - 1]
            dW_S = Z1 * sqrt_dt
            dW_v = (self.rho * Z1 + np.sqrt(1 - self.rho**2) * Z2) * sqrt_dt

            S = paths[:, t - 1]
            v_prev = vol_paths[:, t - 1]

            # Full-truncation scheme (RESEARCH_NOTES §6): use max(v, 0) in both
            # drift and diffusion to prevent negative variance propagation.
            v_plus = np.maximum(v_prev, 0.0)

            vol_paths[:, t] = (
                v_prev
                + self.kappa * (self.theta - v_plus) * dt
                + self.xi * np.sqrt(v_plus) * dW_v
            )
            # Apply truncation to the newly updated variance before using it for S.
            v_cur_plus = np.maximum(vol_paths[:, t], 0.0)

            paths[:, t] = S * np.exp(
                (self.mu - 0.5 * v_cur_plus) * dt + np.sqrt(v_cur_plus) * dW_S
            )

        return paths


@dataclass
class OrnsteinUhlenbeckEuler(PathSimulator):
    """Ornstein-Uhlenbeck process (mean-reversion).

    dX = θ*(μ - X)*dt + σ*dW

    The linear recursion is kept sequential; all random shocks are pre-drawn
    to consolidate RNG calls.
    """

    theta: float = 0.5
    mu: float = 0.0
    sigma: float = 0.2

    def simulate(
        self,
        X0: float,
        T: float,
        n_steps: int,
        n_paths: int,
        rng: np.random.Generator,
        distribution: Distribution,
    ) -> np.ndarray:
        dt = T / n_steps
        paths = np.zeros((n_paths, n_steps + 1))
        paths[:, 0] = X0

        # Pre-draw all shocks — preserves per-step contiguous draw order
        all_dW = distribution.sample(n_paths * n_steps, rng).reshape((n_steps, n_paths)).T
        all_dW *= np.sqrt(dt)

        for t in range(1, n_steps + 1):
            X = paths[:, t - 1]
            dW = all_dW[:, t - 1]
            paths[:, t] = X + self.theta * (self.mu - X) * dt + self.sigma * dW

        return paths


@dataclass
class MertonJumpDiffusionEuler(PathSimulator):
    """Merton jump-diffusion model.

    dS = (μ - λk)S dt + σS dW + S(J-1)dN

    where N is a Poisson process with intensity λ, and
    J ~ exp(μ_J + σ_J * Z) with Z ~ N(0,1).

    The compensated drift (μ - λk) ensures the process is a martingale
    under the risk-neutral measure, where k = E[J-1] = exp(μ_J + σ_J²/2) - 1.
    """

    mu: float = 0.05
    sigma: float = 0.2
    lam: float = 1.0
    mu_jump: float = 0.0
    sigma_jump: float = 0.1

    def simulate(
        self,
        S0: float,
        T: float,
        n_steps: int,
        n_paths: int,
        rng: np.random.Generator,
        distribution: Distribution,
    ) -> np.ndarray:
        dt = T / n_steps
        paths = np.zeros((n_paths, n_steps + 1))
        paths[:, 0] = S0

        # Pre-draw all Brownian shocks
        all_dW = distribution.sample(n_paths * n_steps, rng).reshape((n_steps, n_paths)).T
        all_dW *= np.sqrt(dt)

        # Pre-draw Poisson jump counts per step
        all_jumps = rng.poisson(self.lam * dt, size=(n_paths, n_steps))

        # Pre-draw jump sizes (only used where jumps occur)
        all_jump_sizes = rng.lognormal(self.mu_jump, self.sigma_jump, size=(n_paths, n_steps))

        # Compensator: k = E[J-1] = exp(μ_J + σ_J²/2) - 1
        k = np.exp(self.mu_jump + 0.5 * self.sigma_jump**2) - 1.0

        for t in range(1, n_steps + 1):
            S = paths[:, t - 1]
            dW = all_dW[:, t - 1]
            n_jumps = all_jumps[:, t - 1]
            jump_size = all_jump_sizes[:, t - 1]

            # Diffusion component
            diffusion = (self.mu - self.lam * k) * S * dt + self.sigma * S * dW

            # Compound jump: n_jumps independent lognormal draws → product = J^n
            jump = S * (np.where(n_jumps > 0, jump_size ** np.maximum(n_jumps, 1) - 1.0, 0.0))

            paths[:, t] = S + diffusion + jump

        return paths


@dataclass
class CIREuler(PathSimulator):
    """Cox-Ingersoll-Ross (CIR) model.

    dr = κ(θ - r)dt + σ√r dW

    Uses reflection scheme: max(r, 0) to prevent negative rates.
    """

    kappa: float = 0.5
    theta: float = 0.04
    sigma: float = 0.1

    def simulate(
        self,
        r0: float,
        T: float,
        n_steps: int,
        n_paths: int,
        rng: np.random.Generator,
        distribution: Distribution,
    ) -> np.ndarray:
        dt = T / n_steps
        paths = np.zeros((n_paths, n_steps + 1))
        paths[:, 0] = r0

        # Pre-draw all shocks
        all_dW = distribution.sample(n_paths * n_steps, rng).reshape((n_steps, n_paths)).T
        all_dW *= np.sqrt(dt)

        for t in range(1, n_steps + 1):
            r = paths[:, t - 1]
            dW = all_dW[:, t - 1]

            # Reflection scheme: use max(r, 0) for diffusion coefficient
            r_plus = np.maximum(r, 0.0)

            paths[:, t] = r + self.kappa * (self.theta - r) * dt + self.sigma * np.sqrt(r_plus) * dW

        return paths


def create_simulator(
    model: Literal[
        "gbm_euler",
        "gbm_milstein",
        "heston_euler",
        "ou_euler",
        "merton_euler",
        "cir_euler",
    ],
    model_params: dict,
) -> PathSimulator:
    """Factory to create path simulators.

    S0 is consumed by the call sites (engine.py) and must NOT be forwarded to
    the dataclass constructors, which do not declare it as a field.
    """
    import dataclasses
    cls_map = {
        "gbm_euler": GBMEuler,
        "gbm_milstein": GBMMilstein,
        "heston_euler": HestonEuler,
        "ou_euler": OrnsteinUhlenbeckEuler,
        "merton_euler": MertonJumpDiffusionEuler,
        "cir_euler": CIREuler,
    }
    if model not in cls_map:
        raise ValueError(f"Unknown model: {model}")
    target_cls = cls_map[model]
    allowed_fields = {f.name for f in dataclasses.fields(target_cls)}
    filtered_params = {k: v for k, v in model_params.items() if k in allowed_fields}
    return target_cls(**filtered_params)
