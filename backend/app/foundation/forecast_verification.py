"""Small-sample forecast verification statistics (pure numpy/scipy).

The "Can I trust it?" page has to say something honest about a handful of
resolved calls, so every number here either carries an interval or is an
anytime-valid evidence measure. Nothing in this module touches the database.

What is in here, and the reference each piece follows:

* :func:`hit_rate_ci` — exact Clopper-Pearson interval for a hit rate.
* :func:`brier_score` / :func:`brier_skill` — Brier score and its skill score
  against a reference forecast (by default the base rate), with a
  moving-block bootstrap interval (Kunsch 1989, Liu & Singh 1992). Block
  length ``max(1, round(n ** (1/3)))``, so serially dependent calls do not
  get an interval that is too narrow. Brier & skill scores at small N:
  arXiv:0806.0813.
* :func:`mean_ci_bootstrap` — the same block bootstrap for a mean (excess
  return).
* :func:`e_process_bernoulli` — betting e-process for ``H0: P(hit) <= p0``
  with an aGRAPA predictable bet (Waudby-Smith & Ramdas, arXiv:2010.09686).
  Reading it never needs a fixed sample size: by Ville's inequality the
  chance that the running maximum ever reaches ``1/alpha`` while ``H0`` is
  true is at most ``alpha``, however often somebody looks
  (Shafer 2021 / Ramdas et al., arXiv:2210.01948). Threshold 20 is
  ``alpha = 0.05``, threshold 100 is ``alpha = 0.01``.
* :func:`calls_needed` — how many independent calls a fixed-sample test of
  "hit rate is ``p1``, not ``p0``" needs. Shown as "time to know".
* :func:`spiegelhalter_z` and :func:`reliability_bins` — calibration checks
  for stated probabilities (CORP-style reliability, arXiv:2008.03033; only
  worth drawing from ~30 calls).
* :func:`coverage` — how often a stated range (e.g. an 80 % band) contained
  the outcome, with an exact interval.
* :func:`evidence_state` — the four-way verdict used by the page chips.
* :func:`mean_ci_hac` — interval for a mean under serial correlation:
  Newey-West (1987) Bartlett-kernel standard error with a Student-t quantile.
  Used for the excess return per rebalance date, whose holding windows overlap.
* :func:`e_bh` — the e-BH procedure (Wang & Ramdas 2022, arXiv:2009.02824):
  false-discovery-rate control across several e-values, valid under any
  dependence between them. Several prediction types tested in two directions
  are several looks; a single crossing of 20 is not evidence on its own.

All intervals are two-sided. ``None`` is returned, never a made-up number,
when there is too little data for the quantity to mean anything.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats

#: Below this many calls a bootstrap interval is not reported at all.
MIN_CALLS_FOR_CI = 15
#: E-value at which H0 is rejected at alpha = 0.05 (1 / alpha).
EVIDENCE_THRESHOLD = 20.0
#: E-value at which H0 is rejected at alpha = 0.01.
STRONG_EVIDENCE_THRESHOLD = 100.0
#: Reliability diagrams are only drawn from this many calls.
MIN_CALLS_FOR_RELIABILITY = 30

_DEFAULT_BOOTSTRAP_DRAWS = 2000

EvidenceState = str  # "too_early" | "skill" | "harm" | "no_evidence"


# ---------------------------------------------------------------------------
# Intervals for a proportion
# ---------------------------------------------------------------------------


def hit_rate_ci(hits: int, n: int, level: float = 0.9) -> tuple[float, float] | None:
    """Exact (Clopper-Pearson) two-sided interval for a hit rate.

    Returns ``None`` when ``n == 0``. The interval always contains
    ``hits / n`` and is deliberately conservative: at 5 of 8 it is wide
    (roughly 0.29 to 0.89 at 90 %), which is the honest message.
    """
    if n <= 0:
        return None
    if not 0 <= hits <= n:
        raise ValueError("hits must be between 0 and n")
    _check_level(level)
    alpha = 1.0 - level
    lo = 0.0 if hits == 0 else float(stats.beta.ppf(alpha / 2.0, hits, n - hits + 1))
    hi = 1.0 if hits == n else float(stats.beta.ppf(1.0 - alpha / 2.0, hits + 1, n - hits))
    return lo, hi


@dataclass(frozen=True)
class Coverage:
    """How often a stated range contained the outcome."""

    k: int
    n: int
    rate: float | None
    ci_low: float | None
    ci_high: float | None
    nominal: float


def coverage(inside: Sequence[bool], level: float, ci_level: float = 0.9) -> Coverage:
    """Empirical coverage of a range forecast with an exact interval.

    *level* is the nominal coverage of the stated range (0.8 for a P10-P90
    band); *ci_level* is the confidence of the interval around the observed
    rate. "7 of 10 inside the 80 % range" is ``k=7, n=10``.
    """
    flags = [bool(v) for v in inside]
    n = len(flags)
    k = sum(flags)
    ci = hit_rate_ci(k, n, ci_level) if n else None
    return Coverage(
        k=k,
        n=n,
        rate=(k / n) if n else None,
        ci_low=ci[0] if ci else None,
        ci_high=ci[1] if ci else None,
        nominal=float(level),
    )


# ---------------------------------------------------------------------------
# Moving-block bootstrap
# ---------------------------------------------------------------------------


def block_length(n: int) -> int:
    """Moving-block length ``max(1, round(n ** (1/3)))``."""
    return max(1, int(round(n ** (1.0 / 3.0))))


def _block_index_matrix(n: int, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """``(n_boot, n)`` resample indices from overlapping (moving) blocks."""
    length = min(block_length(n), n)
    n_blocks = math.ceil(n / length)
    starts = rng.integers(0, n - length + 1, size=(n_boot, n_blocks))
    idx = starts[:, :, None] + np.arange(length)[None, None, :]
    return idx.reshape(n_boot, -1)[:, :n]


def _percentile_interval(samples: np.ndarray, level: float) -> tuple[float, float] | None:
    finite = samples[np.isfinite(samples)]
    if finite.size < max(10, samples.size // 2):
        return None
    lo, hi = np.percentile(finite, [(1.0 - level) / 2.0 * 100.0, (1.0 + level) / 2.0 * 100.0])
    return float(lo), float(hi)


def mean_ci_bootstrap(
    x: Sequence[float],
    level: float = 0.9,
    *,
    n_boot: int = _DEFAULT_BOOTSTRAP_DRAWS,
    seed: int = 0,
    min_n: int = MIN_CALLS_FOR_CI,
) -> tuple[float, float] | None:
    """Percentile interval for the mean under a moving-block bootstrap.

    ``None`` below *min_n* observations (default 15).
    """
    _check_level(level)
    arr = np.asarray(list(x), dtype=float)
    n = arr.size
    if n < max(min_n, 2):
        return None
    rng = np.random.default_rng(seed)
    idx = _block_index_matrix(n, n_boot, rng)
    return _percentile_interval(arr[idx].mean(axis=1), level)


# ---------------------------------------------------------------------------
# Brier score and skill
# ---------------------------------------------------------------------------


def _pair(p: Sequence[float], y: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    pa = np.asarray(list(p), dtype=float)
    ya = np.asarray(list(y), dtype=float)
    if pa.shape != ya.shape:
        raise ValueError("p and y must have the same length")
    if pa.size == 0:
        raise ValueError("at least one forecast is required")
    if np.any((pa < 0) | (pa > 1)):
        raise ValueError("probabilities must lie in [0, 1]")
    return pa, ya


def brier_score(p: Sequence[float], y: Sequence[float]) -> float:
    """Mean squared error of probability forecasts ``p`` against outcomes ``y`` (0/1)."""
    pa, ya = _pair(p, y)
    return float(np.mean((pa - ya) ** 2))


@dataclass(frozen=True)
class BrierSkill:
    """Brier score, reference score and skill with an optional interval."""

    n: int
    brier: float
    brier_ref: float
    skill: float | None
    ci_low: float | None
    ci_high: float | None


def brier_skill(
    p: Sequence[float],
    y: Sequence[float],
    p_ref: float | Sequence[float] | None = None,
    *,
    level: float = 0.9,
    n_boot: int = _DEFAULT_BOOTSTRAP_DRAWS,
    seed: int = 0,
    min_n: int = MIN_CALLS_FOR_CI,
) -> BrierSkill:
    """``1 - BS / BS_ref`` with a moving-block bootstrap interval.

    *p_ref* is the naive forecast: ``None`` (default) means the base rate of
    the same sample (climatology; recomputed inside every bootstrap draw), a
    float is a fixed reference probability, and a sequence is a per-call naive
    forecast. Skill is ``None`` when the reference is perfect (for example
    every outcome identical) and the interval is ``None`` below *min_n* calls.
    """
    _check_level(level)
    pa, ya = _pair(p, y)
    n = pa.size
    base_rate_ref = p_ref is None
    if base_rate_ref:
        ref = np.full(n, float(ya.mean()))
    elif np.isscalar(p_ref):
        ref = np.full(n, float(p_ref))  # type: ignore[arg-type]
    else:
        ref = np.asarray(list(p_ref), dtype=float)  # type: ignore[arg-type]
        if ref.shape != pa.shape:
            raise ValueError("p_ref must be a scalar or have the same length as p")

    bs = float(np.mean((pa - ya) ** 2))
    bs_ref = float(np.mean((ref - ya) ** 2))
    skill = None if bs_ref <= 1e-12 else 1.0 - bs / bs_ref

    ci: tuple[float, float] | None = None
    if skill is not None and n >= max(min_n, 2):
        rng = np.random.default_rng(seed)
        idx = _block_index_matrix(n, n_boot, rng)
        err = (pa - ya) ** 2
        bs_b = err[idx].mean(axis=1)
        if base_rate_ref:
            m = ya[idx].mean(axis=1)
            bs_ref_b = m * (1.0 - m)
        else:
            bs_ref_b = ((ref - ya) ** 2)[idx].mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            skill_b = np.where(bs_ref_b > 1e-12, 1.0 - bs_b / bs_ref_b, np.nan)
        ci = _percentile_interval(skill_b, level)

    return BrierSkill(
        n=n,
        brier=bs,
        brier_ref=bs_ref,
        skill=skill,
        ci_low=ci[0] if ci else None,
        ci_high=ci[1] if ci else None,
    )


# ---------------------------------------------------------------------------
# Anytime-valid evidence (e-process)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EProcess:
    """Path of a betting e-process and its running maximum."""

    path: np.ndarray
    running_max: np.ndarray

    @property
    def n(self) -> int:
        return int(self.path.size)

    @property
    def final(self) -> float:
        return float(self.path[-1]) if self.path.size else 1.0

    @property
    def max_value(self) -> float:
        return float(self.running_max[-1]) if self.running_max.size else 1.0

    def crossed(self, threshold: float = EVIDENCE_THRESHOLD) -> bool:
        return self.max_value >= threshold


def e_process_bernoulli(x: Sequence[float] | np.ndarray, p0: float) -> EProcess:
    """Betting e-process for ``H0: P(hit) <= p0`` against a higher hit rate.

    ``E_t = prod_{i<=t} (1 + lambda_i (x_i - p0))`` where the bet ``lambda_i``
    uses only ``x_1 .. x_{i-1}`` (predictable), which is what keeps ``E`` a
    test supermartingale under ``H0``. The bet is aGRAPA::

        lambda_i = clip((mu - p0) / (sigma2 + (mu - p0)^2), 0, 0.5 / p0)

    with ``mu`` and ``sigma2`` the running mean and variance of the earlier
    observations, regularised by one pseudo-observation with mean 0.5 and
    variance 0.25 (so the first bet is 0 when the prior sits on ``p0 = 0.5``).
    Bets are capped at ``0.5 / p0`` so every factor stays at least 0.5.

    Use ``e_process_bernoulli(1 - x, 1 - p0)`` (:func:`e_process_harm`) for
    the opposite direction.
    """
    if not 0.0 < p0 < 1.0:
        raise ValueError("p0 must lie strictly between 0 and 1")
    xs = np.asarray(x, dtype=float)
    if xs.size and (np.any(xs < 0) or np.any(xs > 1)):
        raise ValueError("observations must lie in [0, 1]")

    cap = 0.5 / p0
    path = np.empty(xs.size, dtype=float)
    e_value = 1.0
    total = 0.5  # prior pseudo-observation mean 0.5 with weight 1
    sq_dev = 0.25  # prior pseudo-variance 0.25 with weight 1
    for i, xi in enumerate(xs):
        t = i  # observations seen before this one
        mu = total / (t + 1)
        sigma2 = sq_dev / (t + 1)
        edge = mu - p0
        lam = edge / (sigma2 + edge * edge) if (sigma2 + edge * edge) > 0 else 0.0
        lam = min(max(lam, 0.0), cap)
        e_value *= 1.0 + lam * (xi - p0)
        path[i] = e_value
        total += xi
        mu_now = total / (t + 2)
        sq_dev += (xi - mu_now) ** 2
    running_max = np.maximum.accumulate(path) if path.size else path
    # E_0 = 1 counts towards the maximum: no evidence is "1", never below it.
    running_max = np.maximum(running_max, 1.0)
    return EProcess(path=path, running_max=running_max)


def e_process_harm(x: Sequence[float] | np.ndarray, p0: float) -> EProcess:
    """E-process for ``H0: P(hit) >= p0`` (evidence the hit rate is *below* ``p0``)."""
    return e_process_bernoulli(1.0 - np.asarray(x, dtype=float), 1.0 - p0)


def evidence_state(
    n: int,
    e_skill_max: float,
    e_harm_max: float,
    n_min: int = 20,
    threshold: float = EVIDENCE_THRESHOLD,
) -> EvidenceState:
    """``"skill" | "harm" | "too_early" | "no_evidence"``.

    Harm wins when both directions have crossed the threshold (the record
    changed sign; warning beats reassurance). ``"too_early"`` means fewer than
    *n_min* calls and no crossing yet; with *n_min* or more calls and no
    crossing the honest answer is ``"no_evidence"`` (not "no skill").
    """
    if e_harm_max >= threshold:
        return "harm"
    if e_skill_max >= threshold:
        return "skill"
    if n < n_min:
        return "too_early"
    return "no_evidence"


# ---------------------------------------------------------------------------
# Time to know
# ---------------------------------------------------------------------------


def calls_needed(p1: float, p0: float = 0.5, alpha: float = 0.05, power: float = 0.8) -> int:
    """Independent calls needed to tell hit rate *p1* from *p0* (one-sided z-test).

    ``((z_{1-alpha} sqrt(p0 (1-p0)) + z_power sqrt(p1 (1-p1))) / (p1 - p0))^2``,
    rounded up: 153 calls for 60 % vs 50 %, 617 for 55 %.
    """
    if not 0.0 < p0 < 1.0 or not 0.0 < p1 < 1.0:
        raise ValueError("p0 and p1 must lie strictly between 0 and 1")
    if p1 <= p0:
        raise ValueError("p1 must exceed p0")
    z_alpha = float(stats.norm.ppf(1.0 - alpha))
    z_power = float(stats.norm.ppf(power))
    n = ((z_alpha * math.sqrt(p0 * (1 - p0)) + z_power * math.sqrt(p1 * (1 - p1))) / (p1 - p0)) ** 2
    return int(math.ceil(n - 1e-9))


# ---------------------------------------------------------------------------
# Calibration of stated probabilities
# ---------------------------------------------------------------------------


def spiegelhalter_z(p: Sequence[float], y: Sequence[float]) -> float | None:
    """Spiegelhalter's Z: ``sum (y - p)(1 - 2p) / sqrt(sum (1-2p)^2 p (1-p))``.

    Approximately standard normal when the probabilities are calibrated;
    ``|Z| > 1.96`` flags miscalibration. ``None`` when the statistic is
    undefined (every stated probability is 0, 0.5 or 1).
    """
    pa, ya = _pair(p, y)
    denominator = float(np.sum((1 - 2 * pa) ** 2 * pa * (1 - pa)))
    if denominator <= 1e-12:
        return None
    return float(np.sum((ya - pa) * (1 - 2 * pa)) / math.sqrt(denominator))


def reliability_bins(p: Sequence[float], y: Sequence[float], bins: int = 5) -> list[dict[str, float]]:
    """Equal-count reliability bins: ``[{p_mean, hit_rate, n}]`` ordered by ``p``.

    Stated probabilities from a forecaster are rarely spread over [0, 1], so
    equal-width bins leave most bins empty at small N; equal-count bins keep
    every point plotted. Empty input gives an empty list; callers decide
    whether there are enough calls (see ``MIN_CALLS_FOR_RELIABILITY``) to draw it.
    """
    if bins < 1:
        raise ValueError("bins must be at least 1")
    pa = np.asarray(list(p), dtype=float)
    ya = np.asarray(list(y), dtype=float)
    if pa.shape != ya.shape:
        raise ValueError("p and y must have the same length")
    if pa.size == 0:
        return []
    order = np.argsort(pa, kind="stable")
    out: list[dict[str, float]] = []
    for chunk in np.array_split(order, min(bins, pa.size)):
        if chunk.size == 0:
            continue
        out.append(
            {
                "p_mean": float(pa[chunk].mean()),
                "hit_rate": float(ya[chunk].mean()),
                "n": int(chunk.size),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Serial correlation and multiple testing
# ---------------------------------------------------------------------------

#: Below this many observations a HAC interval is not reported.
MIN_UNITS_FOR_HAC = 8


def mean_ci_hac(
    x: Sequence[float], lag: int, level: float = 0.9, *, min_n: int = MIN_UNITS_FOR_HAC,
) -> tuple[float, float] | None:
    """Newey-West interval for the mean of a serially correlated series.

    ``var(mean) = (g0 + 2 sum_{l=1..L} (1 - l/(L+1)) g_l) / n`` with ``g_l`` the
    lag-``l`` autocovariance and Bartlett weights (always non-negative), and a
    Student-t quantile on ``n - 1`` degrees of freedom. *lag* is how many later
    observations one observation overlaps with (e.g. rebalance dates inside one
    holding window); it is clipped to ``n - 1``. ``None`` below *min_n*.
    """
    _check_level(level)
    arr = np.asarray(list(x), dtype=float)
    n = arr.size
    if n < max(min_n, 2):
        return None
    dev = arr - arr.mean()
    lag = max(0, min(int(lag), n - 1))
    var = float(dev @ dev) / n
    for k in range(1, lag + 1):
        var += 2.0 * (1.0 - k / (lag + 1)) * float(dev[k:] @ dev[:-k]) / n
    se = math.sqrt(max(var, 0.0) / n)
    t = float(stats.t.ppf((1.0 + level) / 2.0, n - 1))
    return float(arr.mean() - t * se), float(arr.mean() + t * se)


def e_bh(e_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Which hypotheses the e-BH procedure rejects at false-discovery rate *alpha*.

    With ``K`` e-values sorted descending, reject the ``k*`` largest where
    ``k* = max{k : e_(k) >= K / (k alpha)}``. One hypothesis reduces to
    "reject when ``e >= 1/alpha``". Valid under arbitrary dependence.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie strictly between 0 and 1")
    values = [float(v) for v in e_values]
    k_total = len(values)
    order = sorted(range(k_total), key=lambda i: values[i], reverse=True)
    k_star = 0
    for rank, i in enumerate(order, start=1):
        if values[i] >= k_total / (rank * alpha):
            k_star = rank
    rejected = set(order[:k_star])
    return [i in rejected for i in range(k_total)]


def _check_level(level: float) -> None:
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie strictly between 0 and 1")
