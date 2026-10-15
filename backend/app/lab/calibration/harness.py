"""The calibration harness (plan 2026-10-14, WP-D2). Pure functions, NumPy only.

The test is ADR 0018 §4's: each day's observation ``x`` is shifted by the
null's margin, clipped to ``±clip``, mapped to ``[0, 1]`` with ``p0 = 0.5``
and bet on with the aGRAPA bettor of ``foundation.forecast_verification``
(pseudo-variance, bet cap). :func:`e_paths` is that bettor run on many paths
at once; ``tests/test_calibration_harness.py`` checks it against
``fv.e_process_bernoulli`` path by path.

The test constants are passed in (:class:`TestSpec`), never imported: the lab
may not read the decision layer. ``scripts/run_calibration.py`` passes
``daily_tests``' own values.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from app.foundation.forecast_verification import variance_ratio

TRADING_DAYS_PER_YEAR = 252
HORIZON_DAYS = 21


@dataclass(frozen=True)
class TestSpec:
    """One hypothesis of one family, as the live test runs it."""

    name: str
    clip: float
    prior_var: float
    cap_factor: float
    threshold: float
    #: "skill" (H0: mean <= shift), "harm" (H0: mean >= shift) or "futility" (H0: mean >= shift).
    direction: str
    #: The null's margin per day (U and M); 0 for skill and harm.
    shift: float = 0.0

    def to_unit(self, x: np.ndarray) -> np.ndarray:
        """Observations on the betting scale: what the e-process sees."""
        y = (np.clip(x - self.shift, -self.clip, self.clip) + self.clip) / (2.0 * self.clip)
        return y if self.direction == "skill" else 1.0 - y


def e_paths(y: np.ndarray, prior_var: float, cap_factor: float, p0: float = 0.5) -> np.ndarray:
    """aGRAPA e-process paths for ``y`` of shape (paths, days); same algebra as
    ``fv.e_process_bernoulli`` (one pseudo-observation, predictable bets)."""
    n_paths, n_days = y.shape
    cap = cap_factor / p0
    out = np.empty_like(y, dtype=float)
    e = np.ones(n_paths)
    total = np.full(n_paths, 0.5)
    sq_dev = np.full(n_paths, float(prior_var))
    for t in range(n_days):
        mu = total / (t + 1)
        sigma2 = sq_dev / (t + 1)
        edge = mu - p0
        denom = sigma2 + edge * edge
        lam = np.where(denom > 0, edge / np.where(denom > 0, denom, 1.0), 0.0)
        lam = np.clip(lam, 0.0, cap)
        xi = y[:, t]
        e = e * (1.0 + lam * (xi - p0))
        out[:, t] = e
        total = total + xi
        mu_now = total / (t + 2)
        sq_dev = sq_dev + (xi - mu_now) ** 2
    return out


def crossing_days(x: np.ndarray, spec: TestSpec) -> np.ndarray:
    """First day (1-based) each path's e-value reaches the threshold; 0 when never."""
    e = e_paths(spec.to_unit(x), spec.prior_var, spec.cap_factor)
    hit = e >= spec.threshold
    first = hit.argmax(axis=1) + 1
    return np.where(hit.any(axis=1), first, 0)


# --- daily series under stated models -------------------------------------------------


def _iid_normal(rng: np.random.Generator, n: int, days: int, mean: float, sd: float) -> np.ndarray:
    return rng.normal(mean, sd, (n, days))


def _t4_garch(rng: np.random.Generator, n: int, days: int, mean: float, sd: float) -> np.ndarray:
    """Student-t(4) shocks with GARCH(1,1) variance (alpha 0.08, beta 0.90), unconditional sd ``sd``."""
    alpha, beta = 0.08, 0.90
    omega = sd * sd * (1 - alpha - beta)
    z = rng.standard_t(4, (n, days)) / np.sqrt(2.0)  # unit variance
    out = np.empty((n, days))
    var = np.full(n, sd * sd)
    for t in range(days):
        eps = np.sqrt(var) * z[:, t]
        out[:, t] = mean + eps
        var = omega + alpha * eps * eps + beta * var
    return out


def _ar1(rng: np.random.Generator, n: int, days: int, mean: float, sd: float, rho: float = 0.15) -> np.ndarray:
    e = rng.normal(0.0, sd * np.sqrt(1 - rho * rho), (n, days))
    out = np.empty((n, days))
    prev = rng.normal(0.0, sd, n)
    for t in range(days):
        prev = rho * prev + e[:, t]
        out[:, t] = mean + prev
    return out


def _regime(rng: np.random.Generator, n: int, days: int, mean: float, sd: float) -> np.ndarray:
    """Two noise regimes (calm 0.7 sd, stressed 1.9 sd; about 20 % of days stressed, sticky), same mean."""
    stay = np.array([0.99, 0.96])
    scale = np.array([0.7, 1.9])
    scale = scale / np.sqrt(0.8 * scale[0] ** 2 + 0.2 * scale[1] ** 2)
    state = np.zeros(n, dtype=int)
    out = np.empty((n, days))
    for t in range(days):
        out[:, t] = mean + rng.normal(0.0, 1.0, n) * sd * scale[state]
        switch = rng.random(n) > stay[state]
        state = np.where(switch, 1 - state, state)
    return out


def _overlap21(rng: np.random.Generator, n: int, days: int, mean: float, sd: float) -> np.ndarray:
    """F1-style membership: 15 picks a week, each open 21 days with its own drift.

    The day's observation is the mean over the ~60 open picks of their daily
    active return; a pick's drift persists over its window, which is what
    makes overlapping membership autocorrelated. Scaled to ``sd``, mean ``mean``.
    Each pick's drift is standard normal, so the mixture already has unit
    variance; the path is never re-centred on its own realised mean (that
    would use future days and remove the drifts' own sampling variation).
    """
    horizon = HORIZON_DAYS
    drift_share = 0.3  # share of the day's variance from the persistent pick drifts
    cohorts = (days + horizon) // 5 + 1
    drifts = rng.normal(0.0, 1.0, (n, cohorts))
    noise = rng.normal(0.0, 1.0, (n, days))
    out = np.empty((n, days))
    for t in range(days):
        first = max(0, (t - horizon) // 5 + 1)
        last = t // 5 + 1
        d = drifts[:, first:last].mean(axis=1) if last > first else 0.0
        out[:, t] = np.sqrt(drift_share) * d * np.sqrt(max(last - first, 1)) + np.sqrt(1 - drift_share) * noise[:, t]
    return mean + sd * out


MODELS: dict[str, Callable[..., np.ndarray]] = {
    "iid_normal": _iid_normal,
    "t4_garch": _t4_garch,
    "ar1_0.15": _ar1,
    "regime_switching": _regime,
    "overlap_21d": _overlap21,
}


def simulate(model: str, n_paths: int, n_days: int, mean_daily: float, sd_daily: float, seed: int) -> np.ndarray:
    """``(n_paths, n_days)`` daily observations under *model*, seeded."""
    return MODELS[model](np.random.default_rng(seed), n_paths, n_days, mean_daily, sd_daily)


def _null_mean(spec: TestSpec) -> float:
    """The boundary of H0, where the false-alarm rate is largest."""
    return spec.shift


def type_i_error(spec: TestSpec, model: str, sd_daily: float, *, years: int = 20, n_paths: int = 2000,
                 seed: int = 1) -> float:
    """Share of null paths whose e-value ever reaches the threshold within *years*."""
    x = simulate(model, n_paths, years * TRADING_DAYS_PER_YEAR, _null_mean(spec), sd_daily, seed)
    return float((crossing_days(x, spec) > 0).mean())


def time_to_verdict(spec: TestSpec, model: str, edge_21d: float, sd_daily: float, *, years: int = 60,
                    n_paths: int = 1000, seed: int = 2) -> dict[str, float | None]:
    """10/50/90 % years to a verdict when the true edge is *edge_21d* beyond the null.

    For skill the mean is ``+edge``; for harm and futility it is ``shift - edge``
    (the side each one is looking for).
    """
    edge_d = edge_21d / HORIZON_DAYS
    mean = edge_d if spec.direction == "skill" else spec.shift - edge_d
    x = simulate(model, n_paths, years * TRADING_DAYS_PER_YEAR, mean, sd_daily, seed)
    days = crossing_days(x, spec).astype(float)
    days[days == 0] = np.inf
    q = np.quantile(days, [0.1, 0.5, 0.9])

    def yrs(v: float) -> float | None:
        return None if not np.isfinite(v) else round(v / TRADING_DAYS_PER_YEAR, 2)

    return {
        "q10_years": yrs(q[0]), "q50_years": yrs(q[1]), "q90_years": yrs(q[2]),
        "p_within_5y": float(np.mean(days <= 5 * TRADING_DAYS_PER_YEAR)),
        "p_within_10y": float(np.mean(days <= 10 * TRADING_DAYS_PER_YEAR)),
    }


def minimum_detectable_edge(spec: TestSpec, model: str, sd_daily: float, years: int,
                            grid_21d: tuple[float, ...], **kw: object) -> float | None:
    """Smallest edge on *grid_21d* whose median time to a verdict is within *years*."""
    for edge in sorted(grid_21d):
        q50 = time_to_verdict(spec, model, edge, sd_daily, years=years, **kw)["q50_years"]  # type: ignore[arg-type]
        if q50 is not None and q50 <= years:
            return edge
    return None


def noise_check_fail_rates(model: str, looks: tuple[tuple[int, float], ...], *, n_paths: int = 20000,
                           sd_daily: float = 0.005, seed: int = 9) -> dict[str, float]:
    """Share of *model* paths the ADR 0018 §11 noise check fails: at the first look and at any look.

    *looks* is ``daily_tests.NOISE_CHECK_LOOKS`` (observed days, VR(21) cutoff),
    passed in like every other test constant. VR ignores the mean, so the
    paths are simulated at mean 0.
    """
    x = simulate(model, n_paths, max(d for d, _ in looks), 0.0, sd_daily, seed)
    fails = np.array([[(variance_ratio(row[:d], HORIZON_DAYS) or 0.0) > cut for d, cut in looks] for row in x])
    return {"first_look": float(fails[:, 0].mean()), "any_look": float(fails.any(axis=1).mean())}
