"""Probability distributions for Monte Carlo simulation.

Supports: Normal, Student-t (heavy-tailed), NIG (Normal-Inverse-Gaussian).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import numpy as np
from scipy import stats
from typing import Optional


@dataclass
class Distribution(ABC):
    """Base class for MC distributions."""

    @abstractmethod
    def sample(self, size: int, rng: np.random.Generator) -> np.ndarray:
        """Draw samples from the distribution."""
        pass

    @abstractmethod
    def pdf(self, x: np.ndarray) -> np.ndarray:
        """Probability density function."""
        pass

    @abstractmethod
    def mean(self) -> float:
        """Mean of the distribution."""
        pass

    @abstractmethod
    def std(self) -> float:
        """Standard deviation."""
        pass


@dataclass
class NormalDistribution(Distribution):
    """Standard normal distribution N(μ, σ²)."""

    mu: float = 0.0
    sigma: float = 1.0

    def sample(self, size: int, rng: np.random.Generator) -> np.ndarray:
        return rng.normal(self.mu, self.sigma, size)

    def pdf(self, x: np.ndarray) -> np.ndarray:
        return stats.norm.pdf(x, self.mu, self.sigma)

    def mean(self) -> float:
        return self.mu

    def std(self) -> float:
        return self.sigma


@dataclass
class StudentTDistribution(Distribution):
    """Student-t distribution for heavy tails: t_ν(μ, σ)."""

    nu: float  # degrees of freedom
    mu: float = 0.0
    sigma: float = 1.0

    def sample(self, size: int, rng: np.random.Generator) -> np.ndarray:
        # numpy's standard_t(nu) has variance nu/(nu-2) for nu>2.
        # To produce shocks with std=sigma we rescale by sqrt((nu-2)/nu):
        #   Y = sigma * sqrt((nu-2)/nu) * z   =>   Var(Y) = sigma^2
        # Guard: for nu<=2 the variance is infinite; we raise to avoid silent NaN.
        if self.nu <= 2:
            raise ValueError(
                f"StudentTDistribution requires nu > 2 for finite variance, got nu={self.nu}"
            )
        z = rng.standard_t(self.nu, size=size)
        return self.mu + self.sigma * np.sqrt((self.nu - 2) / self.nu) * z

    def pdf(self, x: np.ndarray) -> np.ndarray:
        # Scaled Student-t
        z = (x - self.mu) / self.sigma
        return stats.t.pdf(z, self.nu) / self.sigma

    def mean(self) -> float:
        return self.mu if self.nu > 1 else np.nan

    def std(self) -> float:
        if self.nu > 2:
            return self.sigma * np.sqrt(self.nu / (self.nu - 2))
        return np.nan


@dataclass
class NIGDistribution(Distribution):
    """Normal-Inverse-Gaussian: NIG(α, β, μ, δ).

    A flexible heavy-tailed distribution used in finance.
    α: tail parameter (α > |β|)
    β: skewness parameter (-α < β < α)
    μ: location
    δ: scale
    """

    alpha: float
    beta: float
    mu: float = 0.0
    delta: float = 1.0

    def __post_init__(self):
        if self.alpha <= abs(self.beta):
            raise ValueError(f"alpha={self.alpha} must be > |beta|={abs(self.beta)}")

    def sample(self, size: int, rng: np.random.Generator) -> np.ndarray:
        # NIG sampling via inverse-Gaussian + normal mixture
        # IG(μ, λ) sampled, then X ~ N(μ + β*v, v/α²)
        ig_mu = self.delta / np.sqrt(self.alpha**2 - self.beta**2)
        ig_shape = self.delta**2 / (self.alpha**2 - self.beta**2)

        # Inverse-Gaussian sampling (Wald approximation).
        # NOTE: near-boundary instability when alpha ≈ |beta|: ig_shape → 0 and
        # ig_mu → ∞, causing catastrophic cancellation in the expression below.
        # The __post_init__ guard (alpha > |beta|) prevents exact equality but
        # does not protect against very small margins (alpha - |beta| < 1e-6).
        # Callers should keep alpha - |beta| > 1e-4 for numerical stability.
        nu = rng.normal(0, 1, size)
        y = nu**2
        x = ig_mu + (ig_mu**2 * y) / (2 * ig_shape) - (ig_mu / (2 * ig_shape)) * np.sqrt(4 * ig_mu * ig_shape * y + ig_mu**2 * y**2)

        # Mix: keep x or 1-x based on uniform
        u = rng.uniform(0, 1, size)
        v = np.where(u <= ig_mu / (ig_mu + x), x, ig_mu**2 / x)

        return self.mu + self.beta * v + np.sqrt(v / (self.alpha**2)) * rng.normal(0, 1, size)

    def pdf(self, x: np.ndarray) -> np.ndarray:
        # NIG PDF (closed form, computationally intensive)
        gamma = np.sqrt(self.alpha**2 - self.beta**2)
        z = x - self.mu
        numerator = self.alpha * self.delta * np.exp(self.delta * gamma + self.beta * z)
        denominator = np.pi * np.sqrt(self.delta**2 + z**2) * \
                      (self.delta * np.sqrt(self.delta**2 + z**2) + self.delta**2 + z**2) ** 0.5
        return numerator / denominator

    def mean(self) -> float:
        return self.mu + self.delta * self.beta / np.sqrt(self.alpha**2 - self.beta**2)

    def std(self) -> float:
        gamma = np.sqrt(self.alpha**2 - self.beta**2)
        return np.sqrt(self.delta * self.alpha / (gamma**3))


def create_distribution(
    dist_type: str,
    dist_params: Optional[dict] = None,
) -> Distribution:
    """Factory to create distributions by type."""
    if dist_params is None:
        dist_params = {}

    if dist_type == "normal":
        return NormalDistribution(**dist_params)
    elif dist_type == "student_t":
        return StudentTDistribution(**dist_params)
    elif dist_type == "nig":
        return NIGDistribution(**dist_params)
    else:
        raise ValueError(f"Unknown distribution type: {dist_type}")
