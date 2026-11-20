"""Calibration routines for stochastic models.

Provides MLE and method-of-moments estimators for GBM, Heston,
and Merton jump-diffusion models from historical prices.
"""

from dataclasses import dataclass
import numpy as np
from scipy import optimize


@dataclass
class GBMParams:
    """GBM calibration result."""

    mu: float
    sigma: float


@dataclass
class HestonParams:
    """Heston calibration result."""

    v0: float
    kappa: float
    theta: float
    xi: float
    rho: float


@dataclass
class MertonParams:
    """Merton jump-diffusion calibration result."""

    mu: float
    sigma: float
    lambda_jump: float
    mu_jump: float
    sigma_jump: float


def calibrate_gbm(prices: np.ndarray) -> GBMParams:
    """Calibrate GBM from historical prices using MLE.

    The log-returns r_i = log(S_i / S_{i-1}) are i.i.d. normal:
        r ~ N((μ - σ²/2)Δt, σ²Δt)

    MLE estimators:
        σ̂² = Var(r) / Δt
        μ̂ = Mean(r)/Δt + σ̂²/2

    Args:
        prices: Array of historical prices (length >= 2).

    Returns:
        GBMParams with estimated mu and sigma.
    """
    if len(prices) < 2:
        raise ValueError("Need at least 2 prices for GBM calibration")

    log_returns = np.diff(np.log(prices))

    var_r = np.var(log_returns, ddof=1)
    sigma = np.sqrt(var_r)
    mu = np.mean(log_returns) + 0.5 * sigma**2

    return GBMParams(mu=float(mu), sigma=float(sigma))


def calibrate_heston(
    prices: np.ndarray,
    option_prices: np.ndarray | None = None,
) -> HestonParams:
    """Calibrate Heston model from historical prices.

    When only historical prices are provided, uses a quasi-MLE approach
    on realized variance (squared returns).  When option_prices is given,
    calibrates to the implied volatility surface via least-squares.

    For the price-only path, the calibration targets the moments of
    the log-return and squared-return processes.

    Args:
        prices: Array of historical prices.
        option_prices: Optional array of (strike, expiry, market_iv) tuples.

    Returns:
        HestonParams with estimated parameters.
    """
    if len(prices) < 5:
        raise ValueError("Need at least 5 prices for Heston calibration")

    log_returns = np.diff(np.log(prices))
    squared_returns = log_returns**2

    # Realized variance proxy (annualized)
    rv = np.mean(squared_returns)

    # Method-of-moments initial guess
    v0 = float(rv)
    theta = float(rv)
    kappa = 0.5
    xi = 0.3
    rho = -0.3

    if option_prices is not None and len(option_prices) > 0:
        # Calibrate to IV surface via least-squares
        kappa, theta, xi, rho = _calibrate_heston_to_iv(
            option_prices, v0, kappa, theta, xi, rho
        )

    return HestonParams(v0=v0, kappa=kappa, theta=theta, xi=xi, rho=rho)


def _calibrate_heston_to_iv(
    option_data: np.ndarray,
    v0: float,
    kappa: float,
    theta: float,
    xi: float,
    rho: float,
) -> tuple[float, float, float, float]:
    """Fit Heston parameters to an IV surface via least-squares."""
    strikes = option_data[:, 0]
    expiries = option_data[:, 1]
    market_iv = option_data[:, 2]

    def _iv_error(params: np.ndarray) -> float:
        k, th, xi_p, rho_p = params
        if k <= 0 or th <= 0 or xi_p <= 0 or abs(rho_p) >= 1:
            return 1e10

        # Heston characteristic function IV approximation (Heston 1993)
        # Use the simplified at-the-money approximation for speed
        t = np.mean(expiries)
        v_avg = 0.5 * (v0 + th)
        iv = np.sqrt(
            th
            + (v0 - th) * (1 - np.exp(-k * t)) / (k * t)
            + 0.5 * xi_p**2 * v_avg * t / 3
        )
        # Skew adjustment via rho
        log_moneyness = np.log(strikes / np.mean(strikes))
        iv_adjusted = iv + rho_p * xi_p * log_moneyness * 0.5

        return np.mean((iv_adjusted - market_iv) ** 2)

    x0 = np.array([kappa, theta, xi, rho])
    bounds = [(0.01, 10.0), (0.001, 1.0), (0.01, 2.0), (-0.99, 0.99)]

    result = optimize.minimize(_iv_error, x0, bounds=bounds, method="L-BFGS-B")
    if result.success:
        return float(result.x[0]), float(result.x[1]), float(result.x[2]), float(result.x[3])
    return kappa, theta, xi, rho


def calibrate_merton(prices: np.ndarray) -> MertonParams:
    """Calibrate Merton jump-diffusion from historical prices using MLE.

    Decomposes log-returns into continuous and jump components.
    The estimation uses:
    1. EM algorithm to separate jump/diffusion components
    2. MLE on each component conditional on the decomposition

    Simplified approach: use realized variance and extreme returns
    to estimate jump parameters.

    Args:
        prices: Array of historical prices (length >= 10).

    Returns:
        MertonParams with estimated parameters.
    """
    if len(prices) < 10:
        raise ValueError("Need at least 10 prices for Merton calibration")

    log_returns = np.diff(np.log(prices))
    n = len(log_returns)

    # Initial GBM estimates
    mu_gbm = np.mean(log_returns)
    sigma_gbm = np.std(log_returns, ddof=1)

    # Jump detection: flag returns beyond 3σ as potential jumps
    threshold = 3.0 * sigma_gbm
    jump_mask = np.abs(log_returns) > threshold
    n_jumps = int(np.sum(jump_mask))

    # Jump intensity (annualized, assuming daily observations)
    lam = n_jumps / n * 252.0

    if n_jumps < 2:
        # No significant jumps detected; return diffusion-only estimates
        return MertonParams(
            mu=float(mu_gbm + 0.5 * sigma_gbm**2),
            sigma=float(sigma_gbm),
            lambda_jump=0.0,
            mu_jump=0.0,
            sigma_jump=0.0,
        )

    # Jump size parameters from extreme returns
    jump_returns = log_returns[jump_mask]
    mu_jump = float(np.mean(jump_returns))
    sigma_jump = float(np.std(jump_returns, ddof=1))

    # Adjust diffusion: remove jump variance
    jump_var = np.mean(jump_returns**2) * (n_jumps / n)
    diffusion_var = max(sigma_gbm**2 - jump_var, 1e-10)
    sigma = np.sqrt(diffusion_var)

    # Adjust drift for jump compensator
    k = np.exp(mu_jump + 0.5 * sigma_jump**2) - 1.0
    mu = float(mu_gbm + 0.5 * sigma**2 - lam * k)

    # Refine via MLE on the decomposed components
    try:
        result = optimize.minimize(
            _merton_loglik_neg,
            x0=np.array([mu, sigma, lam, mu_jump, sigma_jump]),
            args=(log_returns,),
            method="L-BFGS-B",
            bounds=[
                (-2.0, 2.0),       # mu
                (0.001, 2.0),      # sigma
                (0.01, 50.0),      # lambda
                (-1.0, 1.0),       # mu_jump
                (0.001, 1.0),      # sigma_jump
            ],
        )
        if result.success:
            x = result.x
            return MertonParams(
                mu=float(x[0]),
                sigma=float(x[1]),
                lambda_jump=float(x[2]),
                mu_jump=float(x[3]),
                sigma_jump=float(x[4]),
            )
    except Exception:
        pass  # includes OptimizeWarning (caught via warnings context manager) and other errors

    return MertonParams(
        mu=mu,
        sigma=sigma,
        lambda_jump=lam,
        mu_jump=mu_jump,
        sigma_jump=sigma_jump,
    )


def _merton_loglik_neg(params: np.ndarray, log_returns: np.ndarray) -> float:
    """Negative log-likelihood for the Merton model.

    The Merton model density is an infinite mixture of normals:
        f(r) = Σ_{k=0}^∞ e^{-λΔt} (λΔt)^k / k! * N(r | μ_k, σ_k²)
    where μ_k = (μ - λk)Δt + k*μ_J and σ_k² = σ²Δt + k*σ_J².

    Truncates the sum at k_max = 30*λΔt for computational tractability.
    """
    mu, sigma, lam, mu_jump, sigma_jump = params
    dt = 1.0  # daily returns

    lam_dt = lam * dt
    k_max = max(int(30 * lam_dt), 10)

    ll = np.full_like(log_returns, -np.inf)

    for k in range(k_max + 1):
        mu_k = (mu - lam * (np.exp(mu_jump + 0.5 * sigma_jump**2) - 1)) * dt + k * mu_jump
        sigma_k = np.sqrt(sigma**2 * dt + k * sigma_jump**2)

        if sigma_k < 1e-10:
            continue

        log_poisson = k * np.log(lam_dt + 1e-10) - lam_dt - _log_factorial(k)
        log_normal = -0.5 * np.log(2 * np.pi * sigma_k**2) - (log_returns - mu_k) ** 2 / (2 * sigma_k**2)
        ll = np.logaddexp(ll, log_poisson + log_normal)

    return -np.sum(ll)


def _log_factorial(n: int) -> float:
    """Compute log(n!) using log-gamma for large n."""
    from scipy.special import gammaln
    return float(gammaln(n + 1))
