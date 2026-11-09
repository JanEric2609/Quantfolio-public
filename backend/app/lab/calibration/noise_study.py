"""How noisy each design of the selection and ranking series would be (plan 2026-11-08).

The time an anytime-valid test needs to reach a verdict scales with
``(sigma / edge)^2``: the best possible evidence growth per observation is
``1/2 log(1 + mu^2 / sigma^2)`` (Wang & Ramdas, arXiv 2310.03722, Prop. 6.5),
and sampling more often within the same calendar span adds nothing about the
mean (Merton 1980). So at a given edge, halving a series' noise makes its
verdict about four times faster. This module measures that noise for several
designs on real prices.

It is **outcome-blind**: picks and scores are drawn at random, so nothing here
depends on how Discover's own picks did. A random pick has no edge, which is
what makes the measured standard deviation the test's noise and nothing else.
That lets an amendment choose a design from these figures before any value of
the new series is computed (ADR 0018, amendment of 2026-11-08).

Designs of the selection series (each pick against controls from the same
issue; the observation of a day is the weighted mean over open picks):

* ``sel_sector``: the frozen rule of 2026-10-14, the equal-weighted same-sector
  non-picks (all non-picks below :data:`MIN_CONTROLS`).
* ``sel_knn``: the :data:`KNN` same-sector non-picks nearest in trailing beta,
  volatility and 12-1 momentum (z-scored at issue), the matched-firm idea of
  Barber & Lyon (1997) and the characteristic benchmarks of Daniel, Grinblatt,
  Titman & Wermers (1997).
* ``*_beta``: minus ``(beta_pick - beta_controls) * r_core`` with trailing
  betas; this tests the alpha against the controls, not the raw difference.
* ``*_ivw``: each pick weighted by the inverse of its spread's trailing
  variance (fixed at issue, so predictable and valid under the conditional
  null), the precision weighting of weighted Fama-MacBeth.

Designs of the ranking series (one self-financing cohort per issue, held for
the horizon, the day's value the mean over open cohorts):

* ``rank``: the frozen centred-rank weights (long leg +1, short leg -1).
* ``rank_ef``: the minimum-variance weights with the same exposure to the
  centred rank and zero net weight, under a Ledoit-Wolf covariance of the
  trailing returns: Ledoit, Wolf & Zhao (2019), "Efficient sorting", whose
  t-statistics more than double on 60+ factors. They use DCC-NL; monthly
  rebalancing is where they note a static shrinkage estimator does about as
  well (their Remark 3.2).
* ``*_n100``: the same on a random 100-stock subset, to see what breadth buys.

NumPy and pandas only, no I/O; ``scripts/run_noise_study.py`` loads the prices.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.foundation.evidence_weights import (
    KNN,
    cap_weights,
    near_duplicate_pairs,
    near_duplicates,
    LOOKBACK,
    PRECISION_DAYS,
    centred_rank_weights,
    efficient_weights,
    ledoit_wolf_cov,
    nearest_controls,
    ols_betas,
    precision_weight,
    stock_features,
)

HORIZON = 21
MIN_CONTROLS = 3

SELECTION_DESIGNS = (
    "sel_sector", "sel_sector_beta", "sel_sector_ivw", "sel_sector_beta_ivw",
    "sel_knn", "sel_knn_beta", "sel_knn_ivw", "sel_knn_beta_ivw",
)
RANKING_DESIGNS = ("rank", "rank_ef", "rank_n100", "rank_ef_n100")


@dataclass
class StudyResult:
    """Daily standard deviation per design, per seed, and the derived ratios."""

    sd_daily: dict[str, list[float]] = field(default_factory=dict)
    n_issues: int = 0
    n_days: int = 0
    mean_universe: float = 0.0

    def summary(self, base_selection: str = "sel_sector", base_ranking: str = "rank") -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for name, values in self.sd_daily.items():
            base = base_ranking if name.startswith("rank") else base_selection
            sd = float(np.mean(values))
            ref = float(np.mean(self.sd_daily[base]))
            out[name] = {
                "sd_daily": sd,
                "sd_21d": sd * np.sqrt(HORIZON),
                "sd_vs_base": sd / ref,
                # Time to a verdict at a fixed edge scales with the variance.
                "times_faster": (ref / sd) ** 2,
            }
        return out


def run_study(
    closes: pd.DataFrame,
    core: pd.Series,
    sectors: dict[str, str],
    *,
    n_picks: int = 15,
    step: int = 5,
    seeds: tuple[int, ...] = tuple(range(10)),
) -> StudyResult:
    """Simulate every design on *closes* (trading days x symbols, EUR) with random picks and scores.

    An issue every *step* trading days from the first day with
    :data:`LOOKBACK` days behind it to the last with :data:`HORIZON` ahead.
    A symbol enters an issue's universe when it has a close on each of the
    trailing :data:`LOOKBACK` days and through the horizon. All characteristics
    use data up to the issue day only.
    """
    closes = closes.sort_index()
    core = core.reindex(closes.index).ffill()
    rets = closes.ffill().pct_change(fill_method=None)
    core_r = core.pct_change(fill_method=None).to_numpy()
    r_all = rets.to_numpy()
    symbols = list(closes.columns)
    sector_of = np.array([(sectors.get(s) or "").strip().lower() for s in symbols])
    n_days = len(closes)
    issues = list(range(LOOKBACK + 1, n_days - HORIZON, step))
    result = StudyResult(sd_daily={d: [] for d in (*SELECTION_DESIGNS, *RANKING_DESIGNS)}, n_issues=len(issues))
    universes = []

    # Per-issue characteristics do not depend on the seed: compute once.
    prepared = []
    for t in issues:
        hist = r_all[t - LOOKBACK + 1:t + 1]
        fwd = r_all[t + 1:t + 1 + HORIZON]
        ok = ~np.isnan(hist).any(axis=0) & ~np.isnan(fwd).any(axis=0)
        idx = np.flatnonzero(ok)
        if len(idx) < max(2 * n_picks, 40):
            prepared.append(None)
            continue
        h = hist[:, idx]
        m_hist = core_r[t - LOOKBACK + 1:t + 1]
        if np.isnan(m_hist).any():
            prepared.append(None)
            continue
        beta = ols_betas(h, m_hist)
        feats = stock_features(h, m_hist)
        prepared.append({
            "t": t, "idx": idx, "hist": h, "fwd": fwd[:, idx], "m_hist": m_hist,
            "m_fwd": core_r[t + 1:t + 1 + HORIZON], "beta": beta, "feats": feats,
            "cov": ledoit_wolf_cov(h), "sector": sector_of[idx],
        })
        universes.append(len(idx))
    result.mean_universe = float(np.mean(universes)) if universes else 0.0

    for seed in seeds:
        rng = np.random.default_rng(seed)
        # day index -> design -> list of (value, weight) over open members
        acc: dict[int, dict[str, list[tuple[float, float]]]] = {}
        for p in prepared:
            if p is None:
                continue
            n = len(p["idx"])
            picks = rng.choice(n, size=n_picks, replace=False)
            picked = np.zeros(n, dtype=bool)
            picked[picks] = True
            for i in picks:
                same = np.flatnonzero(~picked & (p["sector"] == p["sector"][i]) & (p["sector"] != ""))
                pool = same if len(same) >= MIN_CONTROLS else np.flatnonzero(~picked)
                knn = nearest_controls(p["feats"], i, pool)
                for ctrl_name, ctrl in (("sector", pool), ("knn", knn)):
                    spread_fwd = p["fwd"][:, i] - p["fwd"][:, ctrl].mean(axis=1)
                    spread_hist = p["hist"][:, i] - p["hist"][:, ctrl].mean(axis=1)
                    db = p["beta"][i] - p["beta"][ctrl].mean()
                    hedged_fwd = spread_fwd - db * p["m_fwd"]
                    hedged_hist = spread_hist - db * p["m_hist"]
                    for beta_tag, fwd_s, hist_s in (("", spread_fwd, spread_hist), ("_beta", hedged_fwd, hedged_hist)):
                        ivw = precision_weight(hist_s) or 0.0
                        for w_tag, w in (("", 1.0), ("_ivw", ivw)):
                            key = f"sel_{ctrl_name}{beta_tag}{w_tag}"
                            for k, v in enumerate(fwd_s):
                                acc.setdefault(p["t"] + 1 + k, {}).setdefault(key, []).append((float(v), w))
            score = rng.standard_normal(n)
            for sub_tag, sub in (("", np.arange(n)), ("_n100", rng.choice(n, size=min(100, n), replace=False))):
                w_rank = centred_rank_weights(score[sub])
                signal = w_rank  # the centred rank, scaled; the exposure target is the rank portfolio's own
                target = float(signal @ w_rank)
                w_ef = efficient_weights(signal, p["cov"][np.ix_(sub, sub)], target)
                for name, w in (("rank", w_rank), ("rank_ef", w_ef)):
                    daily = p["fwd"][:, sub] @ w
                    for k, v in enumerate(daily):
                        acc.setdefault(p["t"] + 1 + k, {}).setdefault(f"{name}{sub_tag}", []).append((float(v), 1.0))
        for design in result.sd_daily:
            series = []
            for day in sorted(acc):
                vals = acc[day].get(design)
                if not vals:
                    continue
                v = np.array([x for x, _ in vals])
                w = np.array([y for _, y in vals])
                series.append(float(v @ w / w.sum()) if w.sum() > 0 else float(v.mean()))
            result.sd_daily[design].append(float(np.std(series, ddof=1)) if len(series) > 1 else float("nan"))
            result.n_days = max(result.n_days, len(series))
    return result


@dataclass(frozen=True)
class RunUniverse:
    """One live Discover run as issued: its evaluable stocks, their sector, composite and pick flag."""

    issue_day: pd.Timestamp
    sector: dict[str, str]
    composite: dict[str, float]
    picked: frozenset[str]


def _selection_weights(
    syms: list[str], run: RunUniverse, est: np.ndarray, m_est: np.ndarray, design: str,
) -> tuple[np.ndarray, float] | None:
    """(stock weights, core weight) of one run's selection portfolio under *design*.

    Each pick contributes ``pick - controls`` (and ``- dbeta * core`` when
    hedged); picks are averaged equally or by the inverse variance of their
    spread over the estimation window.
    """
    col = {s: i for i, s in enumerate(syms)}
    sector = np.array([(run.sector.get(s) or "").strip().lower() for s in syms])
    picked = np.array([s in run.picked for s in syms])
    if not picked.any():
        return None
    beta = ols_betas(est, m_est)
    feats = stock_features(est, m_est)
    parts: list[tuple[np.ndarray, float, float]] = []
    for s in (s for s in syms if s in run.picked):
        i = col[s]
        same = np.flatnonzero(~picked & (sector == sector[i]) & (sector != ""))
        pool = same if len(same) >= MIN_CONTROLS else np.flatnonzero(~picked)
        if not len(pool):
            continue
        ctrl = nearest_controls(feats, i, pool) if "knn" in design else pool
        w = np.zeros(len(syms))
        w[i] += 1.0
        w[ctrl] -= 1.0 / len(ctrl)
        core_w = -float(beta[i] - beta[ctrl].mean()) if "beta" in design else 0.0
        spread = est @ w + core_w * m_est
        prec = (precision_weight(spread) or 0.0) if "ivw" in design else 1.0
        parts.append((w, core_w, prec))
    if not parts:
        return None
    # The ledger's cap: no pick above MAX_WEIGHT_RATIO x the run's median weight.
    precs = cap_weights([p for *_, p in parts]) if "ivw" in design else [p for *_, p in parts]
    weight_sum = float(sum(precs))
    if weight_sum <= 0:
        return None
    w_total = sum(p * w for (w, _, _), p in zip(parts, precs))
    core_total = sum(p * cw for (_, cw, _), p in zip(parts, precs))
    return np.asarray(w_total) / weight_sum, float(core_total) / weight_sum


def ex_ante_study(
    closes: pd.DataFrame, core: pd.Series, runs: list[RunUniverse], *, subset_seed: int = 0,
) -> dict[str, dict[str, float]]:
    """Each design's noise for the runs as actually issued, measured before their issue day.

    Outcome-blind in a second way: the real composites and picks are used, so
    the portfolios carry the factor tilts the real signal has (random scores
    do not), but only returns *before* each issue day enter. Weights come from
    the window ``[t - 2L, t - L)`` and their daily returns are measured on
    ``[t - L, t)``, ``L`` = :data:`LOOKBACK`, so the noise of precision- and
    covariance-weighted designs is not flattered in-sample. Returns, per
    design, the mean over runs of the daily standard deviation of one run's
    portfolio, and the ratios to the frozen designs.
    """
    closes = closes.sort_index()
    rets = closes.ffill().pct_change(fill_method=None)
    core_r = core.reindex(closes.index).ffill().pct_change(fill_method=None)
    sds: dict[str, list[float]] = {d: [] for d in (*SELECTION_DESIGNS, *RANKING_DESIGNS)}
    rng = np.random.default_rng(subset_seed)
    for run in runs:
        t = int(np.searchsorted(closes.index.to_numpy(), np.datetime64(run.issue_day)))
        if t < 2 * LOOKBACK + 1:
            continue
        est_all = rets.iloc[t - 2 * LOOKBACK:t - LOOKBACK]
        ev_all = rets.iloc[t - LOOKBACK:t]
        syms = [s for s in run.composite if s in rets.columns
                and not est_all[s].isna().any() and not ev_all[s].isna().any()]
        if len(syms) < 40:
            continue
        est, ev = est_all[syms].to_numpy(), ev_all[syms].to_numpy()
        m_est = core_r.iloc[t - 2 * LOOKBACK:t - LOOKBACK].to_numpy()
        m_ev = core_r.iloc[t - LOOKBACK:t].to_numpy()
        if np.isnan(m_est).any() or np.isnan(m_ev).any():
            continue
        # The ledger's duplicate rules: a non-pick in a near-duplicate pair is no
        # control; ranking drops the later listing of each pair.
        pairs = near_duplicate_pairs(est)
        twins = {syms[i] for pair in pairs for i in pair if syms[i] not in run.picked}
        sel_idx = [i for i, sym in enumerate(syms) if sym not in twins]
        sel_syms = [syms[i] for i in sel_idx]
        for design in SELECTION_DESIGNS:
            got = _selection_weights(sel_syms, run, est[:, sel_idx], m_est, design)
            if got is not None:
                w, cw = got
                sds[design].append(float(np.std(ev[:, sel_idx] @ w + cw * m_ev, ddof=1)))
        drop = near_duplicates(est)
        keep = [i for i in range(len(syms)) if i not in drop]
        syms, est, ev = [syms[i] for i in keep], est[:, keep], ev[:, keep]
        cov = ledoit_wolf_cov(est)
        score = np.array([run.composite[s] for s in syms])
        sub_all = np.arange(len(syms))
        for tag, sub in (("", sub_all), ("_n100", rng.choice(len(syms), size=min(100, len(syms)), replace=False))):
            w_rank = centred_rank_weights(score[sub])
            w_ef = efficient_weights(w_rank, cov[np.ix_(sub, sub)], float(w_rank @ w_rank))
            sds[f"rank{tag}"].append(float(np.std(ev[:, sub] @ w_rank, ddof=1)))
            sds[f"rank_ef{tag}"].append(float(np.std(ev[:, sub] @ w_ef, ddof=1)))
    out: dict[str, dict[str, float]] = {}
    for design, values in sds.items():
        if not values:
            continue
        base = "rank" if design.startswith("rank") else "sel_sector"
        sd, ref = float(np.mean(values)), float(np.mean(sds[base]))
        out[design] = {
            "n_runs": len(values),
            "sd_daily": sd,
            "sd_21d": sd * np.sqrt(HORIZON),
            "sd_vs_base": sd / ref,
            "times_faster": (ref / sd) ** 2,
        }
    return out


__all__ = [
    "KNN",
    "PRECISION_DAYS",
    "RANKING_DESIGNS",
    "SELECTION_DESIGNS",
    "RunUniverse",
    "StudyResult",
    "ex_ante_study",
    "centred_rank_weights",
    "efficient_weights",
    "run_study",
]
