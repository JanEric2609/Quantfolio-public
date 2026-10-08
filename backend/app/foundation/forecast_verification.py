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
* :func:`e_process_lag_h` — the same e-process made valid for outcomes whose
  windows overlap (Henzi & Ziegel, arXiv:2103.08402).
* :func:`e_process_mean` — the same construction on a bounded mean (the
  basket excess return per date, clipped and betted on).
* :func:`issue_days_needed` / :func:`issue_days_needed_mean` — simulated median issue days until the verdict
  would say "skill" at a true hit rate ``p1``. Shown as "time to know".
  :func:`calls_needed` is the classical fixed-sample z-test number, kept for
  reference only.
* :func:`spiegelhalter_z` and :func:`reliability_bins` — calibration checks
  for stated probabilities (CORP-style reliability, arXiv:2008.03033; only
  worth drawing from ~30 calls).
* :func:`coverage` — how often a stated range (e.g. an 80 % band) contained
  the outcome, with an exact interval.
* :func:`aci_coverage` — adaptive conformal inference (Gibbs & Candès 2021,
  arXiv:2106.00170) over issue dates: how much the stated ranges would have
  to widen to hold their nominal coverage (ADR 0018 §6).
* :func:`evidence_state` — the four-way verdict used by the page chips.
* :func:`mean_ci_hac` — interval for a mean under serial correlation:
  Newey-West (1987) Bartlett-kernel standard error with a Student-t quantile.
  Used for the excess return per rebalance date, whose holding windows overlap.
* :func:`days_to_verdict` — the same "time to know" for the calendar-time
  daily e-process (ADR 0018 §4), as a distribution (10/50/90 %).
* :func:`normal_posterior` — conjugate normal posterior of a mean with a
  HAC likelihood (ADR 0018 §7); shown, never thresholded.
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
from datetime import date
from functools import lru_cache
from typing import Any

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


#: ACI step size (Gibbs & Candès 2021 use 0.005; ADR 0018 §6).
ACI_GAMMA = 0.005
#: Matured weeks before the correction starts (ADR 0018 §6).
ACI_MIN_WEEKS = 30
#: A band may shrink by at most this share of its width on each side, so the
#: corrected low never crosses the corrected high.
_ACI_MIN_WIDEN = -0.49


@dataclass(frozen=True)
class RangeOutcome:
    """One stated range and what happened: the unit the ACI correction reads."""

    issued: date
    matured: date
    low: float
    high: float
    outcome: float


@dataclass(frozen=True)
class AciCoverage:
    """Online conformal correction of stated ranges (ADR 0018 §6).

    ``per_date`` is the raw coverage of each issue date: ``(date, inside, n)``.
    ``widen_now`` is the share of a band's width that the next range would be
    widened by on each side (negative narrows), or ``None`` while inactive.
    ``corrected`` and ``raw_same_dates`` cover the same dates, from the first
    one the correction applied to, so the two read side by side.
    """

    active: bool
    matured_weeks: int
    weeks_needed: int
    alpha_target: float
    alpha_now: float
    gamma: float
    widen_now: float | None
    per_date: list[tuple[date, int, int]]
    corrected: Coverage | None
    raw_same_dates: Coverage | None


def _aci_score(r: RangeOutcome) -> float:
    """Distance outside the band in units of its width (negative inside)."""
    width = r.high - r.low
    if width <= 0:
        return math.inf if not r.low <= r.outcome <= r.high else -math.inf
    return max(r.low - r.outcome, r.outcome - r.high) / width


def _inside(r: RangeOutcome, widen: float) -> bool:
    w = r.high - r.low
    if w <= 0:
        return r.low <= r.outcome <= r.high  # a point forecast: inf * 0 would be NaN
    return r.low - widen * w <= r.outcome <= r.high + widen * w


def _aci_widen(scores: list[float], alpha_t: float) -> float:
    """The (1 - alpha_t) empirical quantile of past scores (Gibbs & Candès)."""
    if alpha_t <= 0:
        return math.inf
    if alpha_t >= 1:
        return _ACI_MIN_WIDEN
    ordered = sorted(scores)
    k = math.ceil((1.0 - alpha_t) * (len(ordered) + 1)) - 1
    if k >= len(ordered):
        return math.inf
    return max(_ACI_MIN_WIDEN, ordered[max(k, 0)])


def aci_coverage(
    ranges: Sequence[RangeOutcome],
    level: float,
    ci_level: float = 0.9,
    *,
    gamma: float = ACI_GAMMA,
    min_weeks: int = ACI_MIN_WEEKS,
    today: date | None = None,
) -> AciCoverage:
    """Adaptive conformal inference over issue dates (Gibbs & Candès 2021).

    The unit is the issue date, as for every other measure on the page: a
    date's error is the share of its outcomes outside the corrected band, and
    ``alpha_{t+1} = alpha_t + gamma * (alpha - err_t)`` is applied once the
    date has matured. The correction for a date reads only dates that matured
    strictly before it was issued, so it never sees its own outcome.

    The score is the distance outside the stated band in units of its width;
    the corrected band is ``[low - q*w, high + q*w]`` with ``q`` the
    ``1 - alpha_t`` quantile of the past scores. Before *min_weeks* distinct
    ISO weeks have matured, ``q = 0`` (the stated band) and alpha does not move.
    """
    _check_level(level)
    alpha = 1.0 - level
    cutoff = today or date.max
    by_date: dict[date, list[RangeOutcome]] = {}
    for r in ranges:
        by_date.setdefault(r.issued, []).append(r)
    # Only dates whose every outcome had matured by the cutoff.
    by_date = {d: rows for d, rows in by_date.items() if max(r.matured for r in rows) <= cutoff}
    dates = sorted(by_date)
    matured_of = {d: max(r.matured for r in by_date[d]) for d in dates}
    per_date = [(d, sum(1 for r in by_date[d] if _inside(r, 0.0)), len(by_date[d])) for d in dates]

    alpha_t = alpha
    applied: dict[date, float] = {}  # widen used for each date, once active
    # Dates processed in issue order; a date's update lands once it matured.
    pending_updates: list[tuple[date, date]] = []  # (matured, issued)
    seen_scores: list[float] = []
    seen_weeks: set[tuple[int, int]] = set()

    def absorb(until: date) -> None:
        nonlocal alpha_t
        for matured, issued in sorted(pending_updates):
            if matured >= until:
                continue
            pending_updates.remove((matured, issued))
            rows = by_date[issued]
            if issued in applied:
                q = applied[issued]
                err = sum(1 for r in rows if not _inside(r, q)) / len(rows)
                alpha_t += gamma * (alpha - err)
            seen_scores.extend(_aci_score(r) for r in rows)
            iso = issued.isocalendar()
            seen_weeks.add((iso[0], iso[1]))

    for d in dates:
        absorb(d)
        if len(seen_weeks) >= min_weeks:
            applied[d] = _aci_widen(seen_scores, alpha_t)
        pending_updates.append((matured_of[d], d))
    absorb(date.max)

    active = len(seen_weeks) >= min_weeks
    corrected = raw = None
    if applied:
        inside_c: list[bool] = []
        inside_r: list[bool] = []
        for d, q in applied.items():
            for r in by_date[d]:
                inside_c.append(_inside(r, q))
                inside_r.append(_inside(r, 0.0))
        corrected = coverage(inside_c, level, ci_level)
        raw = coverage(inside_r, level, ci_level)
    widen_now = _aci_widen(seen_scores, alpha_t) if active else None
    return AciCoverage(
        active=active,
        matured_weeks=len(seen_weeks),
        weeks_needed=min_weeks,
        alpha_target=alpha,
        alpha_now=alpha_t,
        gamma=gamma,
        widen_now=None if widen_now is None or math.isinf(widen_now) else widen_now,
        per_date=per_date,
        corrected=corrected,
        raw_same_dates=raw,
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


def _cluster_index_matrix(blocks: Sequence[int], n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """Row indices of *n_boot* resamples of whole blocks, each draw cut or padded to the sample size."""
    ids = np.asarray(list(blocks))
    groups = [np.flatnonzero(ids == b) for b in np.unique(ids)]
    n = ids.size
    out = np.empty((n_boot, n), dtype=int)
    for b in range(n_boot):
        picked: list[np.ndarray] = []
        total = 0
        while total < n:
            g = groups[int(rng.integers(0, len(groups)))]
            picked.append(g)
            total += g.size
        out[b] = np.concatenate(picked)[:n]
    return out


def brier_skill(
    p: Sequence[float],
    y: Sequence[float],
    p_ref: float | Sequence[float] | None = None,
    *,
    level: float = 0.9,
    n_boot: int = _DEFAULT_BOOTSTRAP_DRAWS,
    seed: int = 0,
    min_n: int = MIN_CALLS_FOR_CI,
    blocks: Sequence[int] | None = None,
) -> BrierSkill:
    """``1 - BS / BS_ref`` with a moving-block bootstrap interval.

    With *blocks* (one block id per call, e.g. five-week windows of issue
    dates), the bootstrap resamples whole blocks instead: calls of one date
    share one market move, so they are never split (ADR 0018 §6).

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
    if skill is not None and n >= max(min_n, 2) and (blocks is None or len(set(blocks)) >= 2):
        rng = np.random.default_rng(seed)
        idx = _block_index_matrix(n, n_boot, rng) if blocks is None else _cluster_index_matrix(blocks, n_boot, rng)
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


def e_process_bernoulli(
    x: Sequence[float] | np.ndarray, p0: float, prior_var: float = 0.25, *, cap_factor: float = 0.5,
) -> EProcess:
    """Betting e-process for ``H0: P(hit) <= p0`` against a higher hit rate.

    ``E_t = prod_{i<=t} (1 + lambda_i (x_i - p0))`` where the bet ``lambda_i``
    uses only ``x_1 .. x_{i-1}`` (predictable), which is what keeps ``E`` a
    test supermartingale under ``H0``. The bet is aGRAPA::

        lambda_i = clip((mu - p0) / (sigma2 + (mu - p0)^2), 0, 0.5 / p0)

    with ``mu`` and ``sigma2`` the running mean and variance of the earlier
    observations, regularised by one pseudo-observation with mean 0.5 and
    variance 0.25 (so the first bet is 0 when the prior sits on ``p0 = 0.5``).
    Bets are capped at ``cap_factor / p0`` (default 0.5) so every factor stays at
    least ``1 - cap_factor``.

    Use ``e_process_bernoulli(1 - x, 1 - p0)`` (:func:`e_process_harm`) for
    the opposite direction.

    The same betting works for any observation in ``[0, 1]`` (the factor is
    linear in ``x``): ``H0: E[x] <= p0``. *prior_var* is the pseudo-variance of
    the regulariser; it is a constant, so the bet stays predictable. A bounded
    mean (see :func:`e_process_mean`) uses a smaller one.
    """
    if not 0.0 < p0 < 1.0:
        raise ValueError("p0 must lie strictly between 0 and 1")
    xs = np.asarray(x, dtype=float)
    if xs.size and (np.any(xs < 0) or np.any(xs > 1)):
        raise ValueError("observations must lie in [0, 1]")

    if not 0.0 < cap_factor < 1.0:
        raise ValueError("cap_factor must lie strictly between 0 and 1")
    cap = cap_factor / p0
    path = np.empty(xs.size, dtype=float)
    e_value = 1.0
    total = 0.5  # prior pseudo-observation mean 0.5 with weight 1
    sq_dev = prior_var  # prior pseudo-variance with weight 1
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


def chain_partition(days: Sequence[date], h: int) -> list[int]:
    """Chain index per unit so that, inside a chain, outcome windows never overlap.

    A unit issued on a date has an outcome window of *h* business days; two units
    can share a chain only when the earlier window has closed
    (``last + h business days <= this date``). Sorted by date, each unit goes to
    the closed chain that closed earliest, else opens a new chain. This is
    greedy interval-graph colouring, which uses the minimum number of chains
    (the largest number of windows open at once). Daily issuing gives *h*
    chains (the interleaving ``index mod h``), weekly issuing about ``h / 5``.
    Returns indices in the order of *days* (which must already be sorted).
    """
    h = max(1, int(h))
    last: list[date] = []
    free_from: list[date] = []  # first date a chain may take another unit
    out: list[int] = []
    for d in days:
        best = -1
        for c, ready in enumerate(free_from):
            if ready <= d and (best < 0 or ready < free_from[best]):
                best = c
        if best < 0:
            best = len(last)
            last.append(d)
            free_from.append(d)
        last[best] = d
        free_from[best] = np.busday_offset(np.datetime64(d), h, roll="forward").astype(object)
        out.append(best)
    return out


def e_process_chains(
    x: Sequence[float] | np.ndarray, chains: Sequence[int] | np.ndarray, p0: float, prior_var: float = 0.25,
    n_chains: int | None = None,
) -> EProcess:
    """Betting e-process that stays valid when neighbouring outcomes overlap.

    Each call's outcome covers a window of several issue days, so the values of
    nearby units overlap and are serially dependent; one betting e-process over
    all of them is then not a test supermartingale. Henzi & Ziegel (Biometrika
    2022, arXiv:2103.08402) handle this with a lag-*h* construction: split the
    sequence into interleaved subsequences whose windows do not overlap, run the
    predictable betting e-process (:func:`e_process_bernoulli`) on each (a test
    supermartingale there), and average them; an average of e-processes is an
    e-process, so Ville's inequality holds for the combined path.

    *chains* generalises the interleaving to an uneven issue schedule
    (:func:`chain_partition`: interval-graph colouring of the windows). The
    schedule is treated as exogenous, it does not depend on the outcomes, so the
    test is conditional on it and the equal weights ``1 / n_chains`` are fixed
    in advance. ``path[t]`` is the average, over all chains, of each chain's
    value after the units up to ``t`` (1 for a chain with no unit yet).
    Deterministic.
    """
    xs = np.asarray(x, dtype=float)
    ch = np.asarray(chains, dtype=int)
    if ch.shape != xs.shape:
        raise ValueError("chains must have one entry per observation")
    if xs.size == 0:
        return e_process_bernoulli(xs, p0, prior_var)
    k = int(n_chains) if n_chains is not None else int(ch.max()) + 1
    k = max(k, int(ch.max()) + 1)
    subs = [e_process_bernoulli(xs[ch == c], p0, prior_var).path for c in range(int(ch.max()) + 1)]
    seen = np.zeros(len(subs), dtype=int)
    total = float(k)
    last = np.ones(len(subs))
    path = np.empty(xs.size, dtype=float)
    for t, c in enumerate(ch):
        new = float(subs[c][seen[c]])
        seen[c] += 1
        total += new - last[c]
        last[c] = new
        path[t] = total / k
    running_max = np.maximum(np.maximum.accumulate(path), 1.0)
    return EProcess(path=path, running_max=running_max)


def e_process_lag_h(
    x: Sequence[float] | np.ndarray, p0: float, h: int, prior_var: float = 0.25,
) -> EProcess:
    """:func:`e_process_chains` for daily issuing: chains are ``unit index mod h``.

    ``h = 1`` is :func:`e_process_bernoulli`.
    """
    xs = np.asarray(x, dtype=float)
    h = max(1, int(h))
    if h == 1 or xs.size == 0:
        return e_process_bernoulli(xs, p0, prior_var)
    return e_process_chains(xs, np.arange(xs.size) % h, p0, prior_var, n_chains=h)


def e_process_lag_h_harm(
    x: Sequence[float] | np.ndarray, p0: float, h: int, prior_var: float = 0.25,
) -> EProcess:
    """Lag-*h* e-process for ``H0: P(hit) >= p0`` (evidence the hit rate is *below* ``p0``)."""
    return e_process_lag_h(1.0 - np.asarray(x, dtype=float), 1.0 - p0, h, prior_var)


def _scale_excess(x: Sequence[float] | np.ndarray, clip: float) -> np.ndarray:
    """Clip to ``[-clip, clip]`` and map to ``[0, 1]`` (0 return -> 0.5)."""
    if clip <= 0:
        raise ValueError("clip must be positive")
    return (np.clip(np.asarray(x, dtype=float), -clip, clip) + clip) / (2.0 * clip)


def mean_prior_var(sd: float, clip: float) -> float:
    """Pseudo-variance of the betting prior for a clipped mean: ``(sd / (2 clip))^2``."""
    return (sd / (2.0 * clip)) ** 2


def e_process_mean(
    x: Sequence[float] | np.ndarray, h: int, clip: float, sd: float, *, harm: bool = False,
    chains: Sequence[int] | None = None,
) -> EProcess:
    """Lag-*h* (or, with *chains*, schedule-aware) betting e-process on a bounded mean, ``H0: E[clip(x)] <= 0`` (skill).

    Each observation (a date's basket excess return) is clipped to
    ``[-clip, clip]`` *before* betting, so the null is about the clipped mean,
    and mapped to ``[0, 1]`` where the null mean is 0.5. Bets are predictable
    aGRAPA plug-ins truncated at 1 (Waudby-Smith & Ramdas, arXiv:2010.09686),
    averaged over *h* interleaved subsequences (Henzi & Ziegel,
    arXiv:2103.08402). ``harm=True`` tests ``H0: E[clip(x)] >= 0`` instead.
    *sd* only sets the regulariser of the first bets. *chains* (from
    :func:`chain_partition`) replaces the ``index mod h`` interleaving for an
    uneven issue schedule; the test is then conditional on that schedule.
    """
    y = _scale_excess(x, clip)
    pv = mean_prior_var(sd, clip)
    if chains is not None:
        return e_process_chains(1.0 - y if harm else y, chains, 0.5, pv)
    return e_process_lag_h_harm(y, 0.5, h, pv) if harm else e_process_lag_h(y, 0.5, h, pv)


MIN_UNITS_FOR_CADENCE = 5


def median_gap_days(days: Sequence[date]) -> float:
    """Median trading days between consecutive issue dates; 1 (daily) below 5 units."""
    if len(days) < MIN_UNITS_FOR_CADENCE:
        return 1.0
    a = np.array(days, dtype="datetime64[D]")
    gaps = np.busday_count(a[:-1], a[1:])
    gaps = gaps[gaps > 0]
    return float(np.median(gaps)) if gaps.size else 1.0


def effective_lag(days: Sequence[date], h: int) -> tuple[int, float]:
    """``(h_eff, median_gap)``: chains needed per *h*-day window at the observed cadence.

    ``h_eff = max(1, ceil(h / median_gap))``: one idea date per week and a
    21-day window need about 5 chains, not 21. Below 5 units daily issuing is assumed.
    """
    gap = median_gap_days(days)
    return max(1, math.ceil(h / gap - 1e-9)), gap


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
    rounded up: 153 calls for 60 % vs 50 %, 617 for 55 %. This is the classical
    fixed-sample number; the page's verdict uses an e-process under e-BH and
    needs far more, so the page shows :func:`issue_days_needed` instead.
    """
    if not 0.0 < p0 < 1.0 or not 0.0 < p1 < 1.0:
        raise ValueError("p0 and p1 must lie strictly between 0 and 1")
    if p1 <= p0:
        raise ValueError("p1 must exceed p0")
    z_alpha = float(stats.norm.ppf(1.0 - alpha))
    z_power = float(stats.norm.ppf(power))
    n = ((z_alpha * math.sqrt(p0 * (1 - p0)) + z_power * math.sqrt(p1 * (1 - p1))) / (p1 - p0)) ** 2
    return int(math.ceil(n - 1e-9))


#: Longest horizon (issue days) the simulation looks at; beyond it the answer is "more than this".
SIM_MAX_DAYS = 15000
SIM_PATHS = 300


def _agrapa_paths(x: np.ndarray, p0: float, prior_var: float = 0.25) -> np.ndarray:
    """Vectorised :func:`e_process_bernoulli`: ``x`` is ``(T, lanes)``, returns the e-value paths."""
    steps, lanes = x.shape
    cap = 0.5 / p0
    total = np.full(lanes, 0.5)
    sq_dev = np.full(lanes, prior_var)
    e_value = np.ones(lanes)
    out = np.empty((steps, lanes))
    for t in range(steps):
        mu = total / (t + 1)
        sigma2 = sq_dev / (t + 1)
        edge = mu - p0
        lam = np.clip(edge / (sigma2 + edge * edge), 0.0, cap)
        e_value = e_value * (1.0 + lam * (x[t] - p0))
        out[t] = e_value
        total = total + x[t]
        sq_dev = sq_dev + (x[t] - total / (t + 2)) ** 2
    return out


def _median_days_to_threshold(
    obs: np.ndarray, p0: float, prior_var: float, k: int, alpha: float, h: int, n_max: int,
) -> int | None:
    """Median first day the lag-*h* averaged e-process on *obs* (``(length, paths*h)``) hits ``k/alpha``."""
    length, lanes = obs.shape
    n_paths = lanes // h
    paths = _agrapa_paths(obs, p0, prior_var).reshape(length, n_paths, h)  # E_k after l+1 obs
    # Sum over subsequences changes by one subsequence's increment per day.
    prev = np.concatenate([np.ones((1, n_paths, h)), paths[:-1]], axis=0)
    diffs = (paths - prev).transpose(1, 0, 2).reshape(n_paths, length * h)[:, :n_max]
    avg = (float(h) + np.cumsum(diffs, axis=1)) / h
    hit = avg >= k / alpha
    reached = hit.any(axis=1)
    if reached.mean() <= 0.5:
        return None
    first = np.where(reached, hit.argmax(axis=1) + 1, n_max + 1)
    return int(np.median(first))


@lru_cache(maxsize=64)
def issue_days_needed(
    p1: float, p0: float = 0.5, k: int = 4, alpha: float = 0.05, h: int = 21,
    *, n_max: int = SIM_MAX_DAYS, n_paths: int = SIM_PATHS, seed: int = 20261005,
) -> int | None:
    """Median issue days until a hit-rate verdict would say "skill" at true hit rate *p1*.

    Simulates (seeded, deterministic) *n_paths* hit-flag sequences, runs the
    lag-*h* averaged e-process (:func:`e_process_lag_h`) and returns the median
    first day it reaches ``k / alpha``, the e-BH threshold for a single
    rejection among *k* tests. Flags are drawn independently: overlap affects
    validity under the null, not the speed at which a real edge shows. ``None``
    when fewer than half the paths get there within *n_max* days. Cached.
    """
    if not 0.0 < p0 < p1 < 1.0:
        raise ValueError("need 0 < p0 < p1 < 1")
    h = max(1, int(h))
    length = math.ceil(n_max / h)
    rng = np.random.default_rng(seed)
    flags = (rng.random((length, n_paths * h)) < p1).astype(float)
    return _median_days_to_threshold(flags, p0, 0.25, k, alpha, h, n_max)


@lru_cache(maxsize=64)
def issue_days_needed_mean(
    edge: float, sd: float, clip: float, k: int = 4, alpha: float = 0.05, h: int = 21,
    *, n_max: int = SIM_MAX_DAYS, n_paths: int = SIM_PATHS, seed: int = 20261005,
) -> int | None:
    """Median issue days until the excess-return verdict (:func:`e_process_mean`) says "skill".

    True per-date basket excess return ~ Normal(*edge*, *sd*), clipped to
    ``+-clip`` like the real observations. Same simulation as
    :func:`issue_days_needed`; ``None`` when not reached within *n_max* days.
    """
    if edge <= 0 or sd <= 0:
        raise ValueError("edge and sd must be positive")
    h = max(1, int(h))
    length = math.ceil(n_max / h)
    rng = np.random.default_rng(seed)
    x = rng.normal(edge, sd, size=(length, n_paths * h))
    return _median_days_to_threshold(_scale_excess(x, clip), 0.5, mean_prior_var(sd, clip), k, alpha, h, n_max)


@dataclass(frozen=True)
class DaysToVerdict:
    """Simulated distribution of the first day a daily e-process reaches its threshold.

    ``q10``/``q50``/``q90`` are trading days, ``None`` when that share of
    paths had not got there within ``n_max`` days ("more than ``n_max``").
    ``p_within`` maps a number of trading days to the share of paths that got
    there by then.
    """

    q10: int | None
    q50: int | None
    q90: int | None
    n_max: int
    p_within: dict[int, float]


@lru_cache(maxsize=64)
def days_to_verdict(
    sharpe_daily: float, sd: float, clip: float, threshold: float, *, prior_var: float, cap_factor: float = 0.5,
    within: tuple[int, ...] = (), n_max: int = SIM_MAX_DAYS, n_paths: int = SIM_PATHS, seed: int = 20261007,
) -> DaysToVerdict:
    """Days until the calendar-time e-process (ADR 0018 §4) says "skill", by simulation.

    Each path is a daily series ``r_s ~ Normal(sharpe_daily * sd, sd)``,
    clipped to ``+-clip``, mapped to ``[0, 1]`` and bet on with the same aGRAPA
    bettor (pseudo-variance *prior_var*, cap ``cap_factor / 0.5``) as the real
    test; the first day the e-value reaches *threshold* is recorded. Daily
    observations of a calendar-time portfolio share no holding window, so they
    are drawn independently. Seeded and cached.
    """
    if sharpe_daily <= 0 or sd <= 0:
        raise ValueError("sharpe_daily and sd must be positive")
    rng = np.random.default_rng(seed)
    p0 = 0.5
    cap = cap_factor / p0
    total = np.full(n_paths, 0.5)
    sq_dev = np.full(n_paths, prior_var)
    e_value = np.ones(n_paths)
    first = np.full(n_paths, n_max + 1)
    alive = np.ones(n_paths, dtype=bool)
    mu_true = sharpe_daily * sd
    for t in range(n_max):
        x = (np.clip(rng.normal(mu_true, sd, n_paths), -clip, clip) + clip) / (2.0 * clip)
        mu = total / (t + 1)
        edge = mu - p0
        lam = np.clip(edge / (sq_dev / (t + 1) + edge * edge), 0.0, cap)
        e_value = e_value * (1.0 + lam * (x - p0))
        total = total + x
        sq_dev = sq_dev + (x - total / (t + 2)) ** 2
        hit = alive & (e_value >= threshold)
        if hit.any():
            first[hit] = t + 1
            alive &= ~hit
            if not alive.any():
                break

    def q(level: float) -> int | None:
        value = float(np.quantile(first, level))
        return None if value > n_max else int(math.ceil(value))

    return DaysToVerdict(
        q10=q(0.1), q50=q(0.5), q90=q(0.9), n_max=n_max,
        p_within={int(d): float(np.mean(first <= d)) for d in within},
    )


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


def spiegelhalter_z_clustered(p: Sequence[float], y: Sequence[float], groups: Sequence[Any]) -> float | None:
    """Spiegelhalter's Z on residuals summed per group (issue date).

    ``R_g = sum_{i in g} (y_i - p_i)(1 - 2 p_i)`` and
    ``Z = sum_g R_g / sqrt(sum_g R_g^2)``: a cluster-robust variance, so the
    calls of one date (one shared market move) are not counted as independent
    and between-date variance is in the denominator (ADR 0018 §6). ``None``
    below two groups or with no variance.
    """
    pa, ya = _pair(p, y)
    ids = list(groups)
    if len(ids) != pa.size:
        raise ValueError("groups must have one entry per call")
    sums: dict[Any, float] = {}
    for g, pi, yi in zip(ids, pa, ya, strict=True):
        sums[g] = sums.get(g, 0.0) + float((yi - pi) * (1 - 2 * pi))
    if len(sums) < 2:
        return None
    r = np.asarray(list(sums.values()))
    denom = float(r @ r)
    return float(r.sum() / math.sqrt(denom)) if denom > 1e-12 else None


@dataclass(frozen=True)
class CorpReliability:
    """CORP reliability (Dimitriadis, Gneiting & Jordan 2021) and the Brier decomposition.

    ``points`` are the PAV blocks ``{p_mean, hit_rate, n}`` (the recalibrated
    curve, ordered by ``p``); ``mcb`` = miscalibration, ``dsc`` =
    discrimination, ``unc`` = uncertainty, with ``Brier = mcb - dsc + unc``.
    """

    points: list[dict[str, float]]
    mcb: float
    dsc: float
    unc: float


def corp_reliability(p: Sequence[float], y: Sequence[float]) -> CorpReliability | None:
    """Pool-adjacent-violators recalibration of *p* against *y*; ``None`` for no calls."""
    if len(p) == 0:
        return None
    pa, ya = _pair(p, y)
    order = np.argsort(pa, kind="stable")
    ps, ys = pa[order], ya[order]
    # PAV on blocks of tied forecasts.
    blocks: list[list[float]] = []  # [sum_y, n, sum_p]
    i = 0
    while i < ps.size:
        j = i
        while j + 1 < ps.size and ps[j + 1] == ps[i]:
            j += 1
        blocks.append([float(ys[i : j + 1].sum()), float(j - i + 1), float(ps[i : j + 1].sum())])
        while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            sy, n, sp = blocks.pop()
            blocks[-1][0] += sy
            blocks[-1][1] += n
            blocks[-1][2] += sp
        i = j + 1
    recal = np.concatenate([np.full(int(n), sy / n) for sy, n, _ in blocks])
    base = float(ys.mean())
    s_orig = float(np.mean((ps - ys) ** 2))
    s_recal = float(np.mean((recal - ys) ** 2))
    s_clim = float(np.mean((base - ys) ** 2))
    return CorpReliability(
        points=[{"p_mean": sp / n, "hit_rate": sy / n, "n": int(n)} for sy, n, sp in blocks],
        mcb=s_orig - s_recal, dsc=s_clim - s_recal, unc=s_clim,
    )


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
    se = math.sqrt(hac_variance_of_mean(arr, lag))
    t = float(stats.t.ppf((1.0 + level) / 2.0, n - 1))
    return float(arr.mean() - t * se), float(arr.mean() + t * se)


def hac_variance_of_mean(x: Sequence[float] | np.ndarray, lag: int) -> float:
    """Newey-West (Bartlett) variance of the sample mean; 0 below two observations."""
    arr = np.asarray(x, dtype=float)
    n = arr.size
    if n < 2:
        return 0.0
    dev = arr - arr.mean()
    lag = max(0, min(int(lag), n - 1))
    var = float(dev @ dev) / n
    for k in range(1, lag + 1):
        var += 2.0 * (1.0 - k / (lag + 1)) * float(dev[k:] @ dev[:-k]) / n
    return max(var, 0.0) / n


def newey_west_lag(n: int) -> int:
    """The usual automatic Bartlett lag ``floor(4 (n/100)^(2/9))`` for *n* observations."""
    return int(math.floor(4.0 * (max(n, 0) / 100.0) ** (2.0 / 9.0))) if n > 0 else 0


def lag1_autocorrelation(x: Sequence[float] | np.ndarray) -> float | None:
    """Sample lag-1 autocorrelation; ``None`` below three observations or with no variance."""
    arr = np.asarray(x, dtype=float)
    if arr.size < 3:
        return None
    dev = arr - arr.mean()
    denom = float(dev @ dev)
    if denom <= 0:
        return None
    return float(dev[1:] @ dev[:-1]) / denom


@dataclass(frozen=True)
class Posterior:
    """Conjugate normal posterior of a mean under the prior ``N(0, tau^2)``."""

    tau: float
    sample_mean: float
    sample_se: float
    shrinkage: float  # kappa = tau^2 / (tau^2 + se^2): the weight on the data
    mean: float
    sd: float
    p_positive: float
    ci_low: float
    ci_high: float


def normal_posterior(
    x: Sequence[float] | np.ndarray, tau: float, lag: int, *, level: float = 0.9, se_floor: float = 0.0,
) -> Posterior | None:
    """Posterior of the mean of *x* with prior ``N(0, tau^2)`` and a Newey-West likelihood.

    The sample mean is treated as ``N(mu, se^2)`` with ``se^2`` the HAC
    variance of the mean (at least ``se_floor^2``), so the posterior is
    normal with ``kappa = tau^2 / (tau^2 + se^2)``, mean ``kappa * xbar`` and
    variance ``kappa * se^2`` (the shrinkage of Jensen, Kelly & Pedersen).
    ``None`` below two observations. Never a test: a posterior that crosses
    95 % at some point is common with no edge at all.
    """
    _check_level(level)
    if tau <= 0:
        raise ValueError("tau must be positive")
    arr = np.asarray(x, dtype=float)
    if arr.size < 2:
        return None
    se2 = max(hac_variance_of_mean(arr, lag), se_floor * se_floor)
    xbar = float(arr.mean())
    if se2 <= 0:
        se2 = 1e-18
    kappa = tau * tau / (tau * tau + se2)
    mean = kappa * xbar
    sd = math.sqrt(kappa * se2)
    z = float(stats.norm.ppf((1.0 + level) / 2.0))
    return Posterior(
        tau=tau, sample_mean=xbar, sample_se=math.sqrt(se2), shrinkage=kappa, mean=mean, sd=sd,
        p_positive=float(stats.norm.cdf(mean / sd)) if sd > 0 else float(mean > 0),
        ci_low=mean - z * sd, ci_high=mean + z * sd,
    )


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
