"""Volatility, VaR and ES forecasts from daily returns (plan 2026-10-14, WP-D3). NumPy and SciPy only.

Two volatility models, averaged in variance:

* **log-HAR** (Corsi 2009), direct form: the log of the mean squared return
  over the next *h* days, regressed on the logs of the last day's, week's and
  month's mean squared return, back-transformed with Duan's smearing factor.
  Daily squared returns stand in for realised
  variance, which needs intraday data the app does not have.
* **GARCH(1,1) with Student-t errors**, fitted by maximum likelihood; the
  *h*-day variance is the sum of the one-step forecasts.

The quantiles are a split-conformal correction (filtered historical
simulation). The models are fitted on the first two thirds of the history, and
the *h*-day returns of the last third are standardised by the forecasts made
before them. VaR and ES are those standardised returns' empirical quantile
and tail mean, scaled by the forecast fitted on the whole history. The mean
return is taken as 0, which is conservative for a loss forecast.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy import optimize, special

#: Floor on squared returns before taking logs (a flat day is not a zero-variance day).
_EPS = 1e-10
#: At least this many daily returns to forecast at all.
MIN_OBS = 500
#: Bump when a model, its inputs or the conformal split changes: it goes into every stored row.
MODEL_VERSION = "har-garcht-conformal-1"


@dataclass(frozen=True)
class GarchT:
    omega: float
    alpha: float
    beta: float
    nu: float

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta


@dataclass(frozen=True)
class RiskForecast:
    """One target's forecast for the next *horizon* trading days; returns are fractions, losses positive."""

    horizon: int
    sigma: float
    var95: float
    var975: float
    es975: float
    n_obs: int
    n_calibration: int
    model_hash: str

    def as_dict(self) -> dict[str, float | int | str]:
        return asdict(self)


def _garch_variances(r: np.ndarray, p: GarchT) -> np.ndarray:
    """sigma^2_t given returns up to t-1, for t = 0..n (the last is the one-step forecast)."""
    n = len(r)
    out = np.empty(n + 1)
    out[0] = float(np.var(r)) if n > 1 else p.omega / max(1e-6, 1.0 - p.persistence)
    for t in range(n):
        out[t + 1] = p.omega + p.alpha * r[t] ** 2 + p.beta * out[t]
    return out


def fit_garch_t(r: np.ndarray) -> GarchT:
    """Maximum-likelihood GARCH(1,1)-t on demeaned daily returns."""
    r = np.asarray(r, dtype=float)
    var = float(np.var(r))

    def nll(theta: np.ndarray) -> float:
        omega, alpha, beta, nu = theta
        if alpha + beta >= 0.999:
            return 1e10
        s2 = _garch_variances(r, GarchT(omega, alpha, beta, nu))[:-1]
        s2 = np.maximum(s2, 1e-12)
        z2 = r ** 2 / (s2 * (nu - 2.0) / nu)
        ll = (special.gammaln((nu + 1) / 2) - special.gammaln(nu / 2) - 0.5 * math.log(math.pi * (nu - 2))
              - 0.5 * np.log(s2) - (nu + 1) / 2 * np.log1p(z2 / nu))
        return float(-np.sum(ll))

    x0 = np.array([var * 0.05, 0.08, 0.90, 6.0])
    bounds = [(var * 1e-4, var * 2.0), (1e-4, 0.4), (0.3, 0.998), (2.5, 50.0)]
    res = optimize.minimize(nll, x0, method="L-BFGS-B", bounds=bounds)
    omega, alpha, beta, nu = (float(v) for v in res.x)
    return GarchT(omega, alpha, beta, nu)


def garch_h_variance(r: np.ndarray, p: GarchT, h: int) -> float:
    """Variance of the sum of the next *h* daily returns."""
    s2_next = _garch_variances(np.asarray(r, dtype=float), p)[-1]
    phi = min(p.persistence, 0.999)
    long_run = p.omega / (1.0 - phi)
    return float(sum(long_run + phi ** k * (s2_next - long_run) for k in range(h)))


def _har_design(r2: np.ndarray) -> np.ndarray:
    """[1, log d, log w, log m] for every day with a month of history (row i is day i + 21)."""
    c = np.concatenate([[0.0], np.cumsum(r2)])
    n = len(r2)
    idx = np.arange(21, n)
    d = r2[idx]
    w = (c[idx + 1] - c[idx - 4]) / 5.0
    m = (c[idx + 1] - c[idx - 21]) / 22.0
    return np.column_stack([np.ones(len(idx)), np.log(d + _EPS), np.log(w + _EPS), np.log(m + _EPS)])


@dataclass(frozen=True)
class Har:
    coef: np.ndarray
    #: Duan's smearing factor, mean(exp(residual)): the log of one squared return is far from normal.
    smear: float


def fit_har(r: np.ndarray, h: int) -> Har:
    """OLS of log(mean r^2 over the next h days) on the HAR regressors."""
    r2 = np.asarray(r, dtype=float) ** 2
    x = _har_design(r2)
    c = np.concatenate([[0.0], np.cumsum(r2)])
    starts = np.arange(21, len(r2)) + 1  # the target window starts the day after the regressors' day
    ok = starts + h <= len(r2)
    y = np.log((c[starts[ok] + h] - c[starts[ok]]) / h + _EPS)
    coef, *_ = np.linalg.lstsq(x[ok], y, rcond=None)
    resid = y - x[ok] @ coef
    return Har(coef=coef, smear=float(np.mean(np.exp(resid))))


def har_h_variance(r: np.ndarray, model: Har, h: int) -> float:
    """Variance of the sum of the next *h* daily returns (smearing back-transform)."""
    x_last = _har_design(np.asarray(r, dtype=float) ** 2)[-1]
    return float(h * math.exp(float(x_last @ model.coef)) * model.smear)


def h_variance(r: np.ndarray, h: int, garch: GarchT, har: Har) -> float:
    """The ensemble: the mean of the two models' *h*-day variances."""
    return 0.5 * (garch_h_variance(r, garch, h) + har_h_variance(r, har, h))


def _standardised(r: np.ndarray, h: int, split: int, garch: GarchT, har: Har) -> np.ndarray:
    """h-day returns after *split*, each over its forecast from the days before it (non-overlapping)."""
    out = []
    for t in range(split, len(r) - h + 1, h):
        sigma = math.sqrt(h_variance(r[:t], h, garch, har))
        out.append(float(np.sum(r[t:t + h])) / sigma)
    return np.asarray(out)


def _model_hash(h: int, n: int) -> str:
    return hashlib.sha256(f"{MODEL_VERSION}|h={h}|n={n}".encode()).hexdigest()[:16]


def forecast(returns: np.ndarray, h: int) -> RiskForecast | None:
    """The *h*-day forecast after the last return, or None with fewer than :data:`MIN_OBS` returns.

    For h = 21 the calibration set has only about n / 63 non-overlapping
    windows; below 20 it falls back to the one-day residuals scaled by sqrt(h).
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < MIN_OBS:
        return None
    r = r - float(np.mean(r))
    split = (2 * len(r)) // 3
    g_fit, h_fit = fit_garch_t(r[:split]), fit_har(r[:split], h)
    z = _standardised(r, h, split, g_fit, h_fit)
    if len(z) < 20:
        g1, h1 = g_fit, fit_har(r[:split], 1)
        z = _standardised(r, 1, split, g1, h1)
    garch, har = fit_garch_t(r), fit_har(r, h)
    sigma = math.sqrt(h_variance(r, h, garch, har))
    q05, q025 = np.quantile(z, [0.05, 0.025])
    tail = z[z <= q025]
    es_z = float(np.mean(tail)) if len(tail) else float(q025)
    return RiskForecast(
        horizon=h,
        sigma=sigma,
        var95=max(0.0, -sigma * float(q05)),
        var975=max(0.0, -sigma * float(q025)),
        es975=max(0.0, -sigma * es_z),
        n_obs=len(r),
        n_calibration=len(z),
        model_hash=_model_hash(h, len(r)),
    )
