"""Quasi-Monte Carlo via Sobol sequences with Owen scrambling."""

from dataclasses import dataclass
import numpy as np
from scipy.stats.qmc import Sobol


@dataclass
class SobolGenerator:
    """Generates Sobol quasi-random sequences with Owen scrambling.

    Uses scipy.stats.qmc.Sobol which has proper direction numbers for any
    dimension and uses proper Owen scrambling (digital net permutation).
    """

    dim: int = 1
    scramble_seed: int | None = None

    def __post_init__(self) -> None:
        if self.dim < 1:
            raise ValueError(f"dim must be >= 1, got {self.dim}")
        if self.scramble_seed is not None:
            self._sobol = Sobol(d=self.dim, scramble=True, rng=self.scramble_seed)
        else:
            self._sobol = Sobol(d=self.dim, scramble=False)

    def generate(self, n: int) -> np.ndarray:
        """Generate n points in [0, 1)^dim using the Sobol sequence.

        Args:
            n: Number of quasi-random points.

        Returns:
            Array of shape (n, dim) with values in [0, 1).
        """
        return self._sobol.random(n)

    def generate_uniform(self, n: int, low: float = 0.0, high: float = 1.0) -> np.ndarray:
        """Generate n points uniformly distributed in [low, high)^dim."""
        return low + (high - low) * self.generate(n)


def generate_sobol_paths(
    n_paths: int,
    n_steps: int,
    mu: float,
    sigma: float,
    S0: float,
    dt: float,
    scramble_seed: int | None = None,
) -> np.ndarray:
    """Generate GBM paths using Sobol quasi-random draws.

    Replaces the normal random draws with Sobol-drawn values mapped
    through the inverse normal CDF for QMC convergence.

    Args:
        n_paths: Number of simulation paths.
        n_steps: Number of time steps.
        mu: Drift parameter.
        sigma: Diffusion parameter.
        S0: Initial price.
        dt: Time step size.
        scramble_seed: Seed for Owen scrambling (None for no scrambling).

    Returns:
        Array of shape (n_paths, n_steps + 1) with price paths.
    """
    from scipy.stats import norm

    sobol = SobolGenerator(dim=n_steps, scramble_seed=scramble_seed)
    # Generate n_paths quasi-random points, each with n_steps dimensions
    uniform = sobol.generate(n_paths)  # (n_paths, n_steps)
    # Map to standard normal
    z = norm.ppf(np.clip(uniform, 1e-10, 1 - 1e-10))

    dW = z * np.sqrt(dt)

    drift = (mu - 0.5 * sigma**2) * dt
    log_returns = drift + sigma * dW
    cumsum = np.cumsum(log_returns, axis=1)

    paths = np.zeros((n_paths, n_steps + 1))
    paths[:, 0] = S0
    paths[:, 1:] = S0 * np.exp(cumsum)

    return paths
