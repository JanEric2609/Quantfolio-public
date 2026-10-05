"""Greeks computation via pathwise IPA and likelihood ratio methods."""

from dataclasses import dataclass
import numpy as np
from typing import Optional


@dataclass
class GreeksEstimate:
    """Container for Greeks estimates with standard errors.

    gamma, gamma_stderr, vega, vega_stderr are Optional because some estimators
    (e.g. pathwise IPA in its current generic form) can compute delta but not
    second-order sensitivities without additional model parameters.  A None value
    means "not computed" — callers must not treat it as zero.
    """

    delta: float
    delta_stderr: float
    gamma: Optional[float]
    gamma_stderr: Optional[float]
    vega: Optional[float]
    vega_stderr: Optional[float]
    theta: Optional[float] = None
    rho: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "delta": self.delta,
            "delta_stderr": self.delta_stderr,
            "gamma": self.gamma,
            "gamma_stderr": self.gamma_stderr,
            "vega": self.vega,
            "vega_stderr": self.vega_stderr,
            "theta": self.theta,
            "rho": self.rho,
        }


def compute_greeks_pathwise(
    S0: float,
    payoff_values: np.ndarray,
    paths: np.ndarray,
    dS0_dpaths: np.ndarray,
    *,
    K: Optional[float] = None,
    r: float = 0.05,
    sigma: Optional[float] = None,
    T: Optional[float] = None,
) -> GreeksEstimate:
    """Compute Greeks via pathwise infinitesimal perturbation analysis (IPA).

    Implements the Glasserman (2004) §7.2 formulas for a European call under GBM:

        Delta = e^{-rT} * mean(S_T / S_0  *  1_{S_T > K})
        Gamma = e^{-rT} * mean(1_{S_T > K} * S_T / (S_0^2 * sigma^2 * T))
        Vega  = e^{-rT} * mean(1_{S_T > K} * S_T * (log(S_T/S_0) - (r + sigma^2/2)*T) / sigma)

    When K, sigma, T are not supplied the function falls back to a generic
    estimator: delta uses S_0 as a proxy for K and gamma/vega are returned as
    None.

    Args:
        S0: Initial price
        payoff_values: Evaluated payoffs shape (n_paths,)
        paths: Price paths shape (n_paths, n_steps+1)
        dS0_dpaths: Sensitivity matrix d(paths)/dS0 (kept for API compat, unused)
        K: Strike price (optional; enables full IPA formulas)
        r: Risk-free rate (default 0.05)
        sigma: Volatility (optional; enables gamma and vega)
        T: Time to maturity in years (optional; enables gamma and vega)
    """
    n_paths = len(payoff_values)
    S_T = paths[:, -1]

    if K is not None and sigma is not None and T is not None:
        # Full Glasserman (2004) IPA formulas for European call on GBM.
        discount = np.exp(-r * T)
        indicator = (S_T > K).astype(float)

        # Delta = e^{-rT} * mean(S_T/S_0 * 1{S_T > K})
        delta_values = discount * (S_T / S0) * indicator
        delta = float(np.mean(delta_values))
        delta_stderr = float(np.std(delta_values) / np.sqrt(n_paths))

        # Gamma = e^{-rT} * mean(1{S_T > K} * S_T / (S_0^2 * sigma^2 * T))
        gamma_values = discount * indicator * S_T / (S0**2 * sigma**2 * T)
        gamma = float(np.mean(gamma_values))
        gamma_stderr = float(np.std(gamma_values) / np.sqrt(n_paths))

        # Vega = e^{-rT} * mean(1{S_T > K} * S_T * (log(S_T/S_0) - (r + sigma^2/2)*T) / sigma)
        log_ratio = np.log(np.maximum(S_T / S0, 1e-15))
        vega_values = (
            discount * indicator * S_T * (log_ratio - (r + 0.5 * sigma**2) * T) / sigma
        )
        vega = float(np.mean(vega_values))
        vega_stderr = float(np.std(vega_values) / np.sqrt(n_paths))
    else:
        # Fallback: only delta is computable without full model parameters.
        # Use S_0 as proxy for K; omit discount factor (r and T unknown).
        indicator = (S_T > S0).astype(float)
        delta_values = (S_T / S0) * indicator
        delta = float(np.mean(delta_values))
        delta_stderr = float(np.std(delta_values) / np.sqrt(n_paths))
        gamma = None
        gamma_stderr = None
        vega = None
        vega_stderr = None

    return GreeksEstimate(
        delta=delta,
        delta_stderr=delta_stderr,
        gamma=gamma,
        gamma_stderr=gamma_stderr,
        vega=vega,
        vega_stderr=vega_stderr,
    )


def compute_greeks_likelihood_ratio(
    S0: float,
    payoff_values: np.ndarray,
    paths: np.ndarray,
    sigma: float,
    T: float,
    r: float = 0.05,
) -> GreeksEstimate:
    """Compute Greeks via the likelihood ratio (score function) method.

    For GBM under the risk-neutral measure the log-normal score is:

        z_i = (log(S_T_i / S_0) - (r - 0.5*σ²)*T) / (σ * sqrt(T))

    Delta estimator (Broadie & Glasserman 1996):

        Δ_LR = e^{-rT} * mean( payoff_i * z_i / (S_0 * σ * sqrt(T)) )

    Gamma estimator:

        Γ_LR = e^{-rT} * mean( payoff_i * (z_i² - 1) / (S_0² * σ² * T) )
               - 2 * Δ_LR / S_0          # correction term

    Note: The correction term is only exact for the standard GBM call; for a
    general payoff it is an approximation.  A TODO is left to add the exact
    second-order formula when model parameters are fully known.

    Vega estimator:

        V_LR = e^{-rT} * mean( payoff_i * (z_i² - z_i * σ * sqrt(T) - 1) / σ )

    Useful when payoff is non-differentiable (e.g., digital options) because
    the score function does not depend on payoff differentiability.
    """
    n_paths = len(payoff_values)
    S_T = paths[:, -1]

    sig_sqrt_T = sigma * np.sqrt(T)
    discount = np.exp(-r * T)

    # Standardised log-return z_i under risk-neutral GBM
    log_ret = np.log(S_T / S0)
    drift_adj = (r - 0.5 * sigma**2) * T
    z = (log_ret - drift_adj) / sig_sqrt_T  # shape (n_paths,)

    # Delta: e^{-rT} * E[ f_i * z_i / (S_0 * σ * sqrt(T)) ]
    delta_values = discount * payoff_values * z / (S0 * sig_sqrt_T)
    delta = float(np.mean(delta_values))
    delta_stderr = float(np.std(delta_values) / np.sqrt(n_paths))

    # Gamma: e^{-rT} * E[ f_i * (z_i^2 - 1) / (S_0^2 * σ^2 * T) ]
    # TODO: add the exact boundary correction for non-call payoffs.
    gamma_values = discount * payoff_values * (z**2 - 1.0) / (S0**2 * sigma**2 * T)
    gamma = float(np.mean(gamma_values))
    gamma_stderr = float(np.std(gamma_values) / np.sqrt(n_paths))

    # Vega: e^{-rT} * E[ f_i * (z_i^2 - z_i * σ * sqrt(T) - 1) / σ ]
    vega_values = discount * payoff_values * (z**2 - z * sig_sqrt_T - 1.0) / sigma
    vega = float(np.mean(vega_values))
    vega_stderr = float(np.std(vega_values) / np.sqrt(n_paths))

    return GreeksEstimate(
        delta=delta,
        delta_stderr=delta_stderr,
        gamma=gamma,
        gamma_stderr=gamma_stderr,
        vega=vega,
        vega_stderr=vega_stderr,
    )


def compute_black_scholes_greeks(
    S0: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> dict:
    """Compute analytical Black-Scholes Greeks for European options.

    Used as a benchmark to validate MC estimates.
    """
    from scipy.stats import norm

    d1 = (np.log(S0 / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == "call":
        delta = norm.cdf(d1)
        gamma = norm.pdf(d1) / (S0 * sigma * np.sqrt(T))
        vega = S0 * norm.pdf(d1) * np.sqrt(T) / 100
        theta = (
            -S0 * norm.pdf(d1) * sigma / (2 * np.sqrt(T))
            - r * K * np.exp(-r * T) * norm.cdf(d2)
        ) / 365
    else:  # put
        delta = norm.cdf(d1) - 1
        gamma = norm.pdf(d1) / (S0 * sigma * np.sqrt(T))
        vega = S0 * norm.pdf(d1) * np.sqrt(T) / 100
        theta = (
            -S0 * norm.pdf(d1) * sigma / (2 * np.sqrt(T))
            + r * K * np.exp(-r * T) * norm.cdf(-d2)
        ) / 365

    rho = K * T * np.exp(-r * T) * (norm.cdf(d2) if option_type == "call" else -norm.cdf(-d2)) / 100

    return {
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "rho": rho,
    }
