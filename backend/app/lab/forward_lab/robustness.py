"""Evidence and robustness checks on a daily series (plan 2026-11-09, §3 and §4).

Pure NumPy, no I/O. The e-process is the trust page's (ADR 0018): observations
clipped and mapped to [0, 1], the aGRAPA bettor with the same pseudo-variance
and cap. One difference: the clip here is 4.5 times the standard deviation of
the *earlier* observations (no bet before :data:`MIN_OBS`), because a lab
signal has no measured noise fixed in advance. The clip is known before each
day, so the bet stays predictable and the e-process valid.

The robustness checks are what the idle server is for. They re-ask, many
times over, how easily the evidence could come from noise:

* :func:`null_false_alarm`: the series with its mean removed, resampled in
  blocks (keeping its volatility clustering and autocorrelation) a thousand
  times; the share of resamples whose e-value reaches the threshold. Near the
  nominal rate means the test is honest on this series' own noise.
* :func:`without_best_days`: the evidence with the five best days removed: a
  record carried by a handful of days is fragile.
* :func:`halves`: the evidence from the first and the second half separately.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.lab.calibration.harness import e_paths

#: The trust page's bettor settings (ADR 0018): pseudo-variance and cap.
PRIOR_VAR = 0.0121
BET_CAP = 0.75
CLIP_SD = 4.5
#: Days observed before the first bet (the clip needs a standard deviation).
MIN_OBS = 20
#: Error rate of the family of lab signals, controlled with e-BH.
ALPHA = 0.01
BOOTSTRAP_DRAWS = 1000
#: Resamples in the nightly run, where the time is free (about 3 s per signal at 10 years).
NIGHTLY_DRAWS = 10_000
BEST_DAYS_REMOVED = 5


def to_unit_predictable(x: np.ndarray, min_obs: int = MIN_OBS, clip_sd: float = CLIP_SD) -> np.ndarray:
    """Rows of *x* (paths x days) mapped to [0, 1] with a clip from earlier days only.

    Returns the columns from *min_obs* on: day t is clipped at ``clip_sd`` times
    the sample standard deviation of days ``0 .. t-1``.
    """
    x = np.atleast_2d(np.asarray(x, dtype=float))
    n = x.shape[1]
    if n <= min_obs:
        return np.empty((x.shape[0], 0))
    csum = np.cumsum(x, axis=1)
    csq = np.cumsum(x * x, axis=1)
    t = np.arange(1, n + 1, dtype=float)
    # Moments of days 0..t-1, used on day t.
    mean_prev = csum[:, :-1] / t[:-1]
    var_prev = (csq[:, :-1] - t[:-1] * mean_prev**2) / np.maximum(t[:-1] - 1.0, 1.0)
    sd_prev = np.sqrt(np.maximum(var_prev, 0.0))
    c = clip_sd * sd_prev[:, min_obs - 1 :]
    c = np.where(c > 0, c, np.inf)
    xs = x[:, min_obs:]
    clipped = np.clip(xs, -c, c)
    y = np.where(np.isfinite(c), (clipped + c) / (2.0 * np.where(np.isfinite(c), c, 1.0)), 0.5)
    return y


def _unit(x: np.ndarray, clip: float | None) -> np.ndarray:
    """Betting scale: a fixed *clip* (the trust page's tests) or the predictable one (the lab)."""
    x = np.atleast_2d(np.asarray(x, dtype=float))
    if clip is None:
        return to_unit_predictable(x)
    return (np.clip(x, -clip, clip) + clip) / (2.0 * clip)


def e_values(values: list[float] | np.ndarray, clip: float | None = None) -> tuple[float, float]:
    """(skill, harm): the current e-values that the mean is above / below zero."""
    y = _unit(np.asarray(values, dtype=float)[None, :], clip)
    if y.shape[1] == 0:
        return 1.0, 1.0
    skill = e_paths(y, PRIOR_VAR, BET_CAP)[0, -1]
    harm = e_paths(1.0 - y, PRIOR_VAR, BET_CAP)[0, -1]
    return float(skill), float(harm)


def e_bh(e: dict[str, float], alpha: float = ALPHA) -> set[str]:
    """Names e-BH rejects (Wang & Ramdas 2022): valid under any dependence between signals."""
    k_total = len(e)
    ranked = sorted(e.items(), key=lambda kv: -kv[1])
    accepted = 0
    for k, (_name, value) in enumerate(ranked, start=1):
        if value >= k_total / (alpha * k):
            accepted = k
    return {name for name, _ in ranked[:accepted]}


def _block_bootstrap(x: np.ndarray, draws: int, rng: np.random.Generator) -> np.ndarray:
    n = len(x)
    block = max(1, int(round(n ** (1.0 / 3.0))))
    n_blocks = -(-n // block)
    starts = rng.integers(0, n - block + 1, size=(draws, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(draws, -1)[:, :n]
    return x[idx]


def null_false_alarm(
    values: list[float] | np.ndarray, threshold: float, *, draws: int = BOOTSTRAP_DRAWS, seed: int = 0,
    clip: float | None = None,
) -> float | None:
    """Share of mean-removed block resamples whose e-value (either side) ever reaches *threshold*."""
    x = np.asarray(values, dtype=float)
    if len(x) <= MIN_OBS + 5:
        return None
    rng = np.random.default_rng(seed)
    paths = _block_bootstrap(x - x.mean(), draws, rng)
    y = _unit(paths, clip)
    skill = e_paths(y, PRIOR_VAR, BET_CAP).max(axis=1)
    harm = e_paths(1.0 - y, PRIOR_VAR, BET_CAP).max(axis=1)
    return float(np.mean((skill >= threshold) | (harm >= threshold)))


def without_best_days(
    values: list[float] | np.ndarray, k: int = BEST_DAYS_REMOVED, clip: float | None = None,
) -> float:
    """The skill e-value with the *k* largest days removed (order otherwise kept)."""
    x = np.asarray(values, dtype=float)
    if len(x) <= k:
        return 1.0
    keep = np.ones(len(x), dtype=bool)
    keep[np.argsort(x)[-k:]] = False
    return e_values(x[keep], clip)[0]


def halves(values: list[float] | np.ndarray, clip: float | None = None) -> tuple[float, float]:
    """Skill e-values of the first and the second half on their own."""
    x = np.asarray(values, dtype=float)
    mid = len(x) // 2
    return e_values(x[:mid], clip)[0], e_values(x[mid:], clip)[0]


@dataclass(frozen=True)
class Robustness:
    false_alarm: float | None
    without_best_days: float
    first_half: float
    second_half: float

    def as_dict(self) -> dict[str, float | None]:
        return {
            "false_alarm": self.false_alarm,
            "without_best_days": self.without_best_days,
            "first_half": self.first_half,
            "second_half": self.second_half,
        }


def robustness(
    values: list[float] | np.ndarray, threshold: float, *, seed: int = 0, clip: float | None = None,
    draws: int = BOOTSTRAP_DRAWS,
) -> Robustness:
    """All three checks; *clip* fixed for a trust-page series, ``None`` for a lab signal."""
    first, second = halves(values, clip)
    return Robustness(
        false_alarm=null_false_alarm(values, threshold, seed=seed, clip=clip, draws=draws),
        without_best_days=without_best_days(values, clip=clip),
        first_half=first,
        second_half=second,
    )


__all__ = [
    "ALPHA",
    "Robustness",
    "e_bh",
    "e_values",
    "halves",
    "null_false_alarm",
    "robustness",
    "to_unit_predictable",
    "without_best_days",
]
