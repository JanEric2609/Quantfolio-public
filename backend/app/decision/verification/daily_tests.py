"""The pre-registered calendar-time tests (ADR 0018 §2, §4, §7).

Reads the frozen ``trust_daily_active_returns`` series (``daily_ledger.py``)
and answers, per family:

* **F1 ideas** (primary, drives the headline): do Discover's picks, held as
  issued, beat the passive core ETF?
* **F2 ranking** (primary, its own row): does the composite sort winners from
  losers among every scored stock?
* **F3 advisor** (secondary): do the advisor's directional calls beat the ETF?

Each family tests skill and harm with e-BH at 5 % (K = 2, so one rejection
needs e >= 40). The observation of day *s* is clipped to +-:data:`CLIP` and bet
on with the aGRAPA bettor of ``foundation.forecast_verification`` (pseudo-
variance :data:`PRIOR_VAR`, bet cap ``0.75 / p0``). Only days on or after
:data:`PRIMARY_TEST_START` enter an e-value; earlier rows are shown as "before
pre-registration". A day with nothing open carries no bet and is skipped.

Every constant here is fixed by the ADR. Changing one is an amendment that
must say what had been observed when it was made.

Shown next to the test, never instead of it:

* the posterior of the mean daily active return (prior N(0, tau^2), tau =
  0.5 % per 21 days, HAC likelihood) with a tau sensitivity strip;
* the simulated "time to know" (10/50/90 %) at stated assumptions. Measured
  values (the series' own standard deviation, after :data:`MIN_DAYS_FOR_STATE`
  days) feed only this forecast, never the test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.decision.verification.daily_ledger import SERIES, SERIES_ADVISOR, SERIES_IDEAS, SERIES_RANKING
from app.foundation import forecast_verification as fv
from app.foundation.models.entities import TrustDailyActiveReturn

#: First day an observation may enter an e-value (ADR 0018 §2: 2026-10-12, or
#: the first trading day after the ADR merges if that is later).
PRIMARY_TEST_START = date(2026, 10, 12)
#: Reference daily standard deviation: 5 % per 21 days.
SIGMA_REF = 0.05 / math.sqrt(21)
#: Clip before betting: 4.5 x SIGMA_REF, rounded to 5 %.
CLIP = 0.05
#: aGRAPA pseudo-variance on the [0, 1] scale: (SIGMA_REF / 2 CLIP)^2, rounded.
PRIOR_VAR = 0.0121
#: Bet cap as a share of 1 / p0: every factor stays >= 0.25.
BET_CAP = 0.75
FDR_LEVEL = 0.05
#: Hypotheses per family: skill and harm.
FAMILY_K = 2
THRESHOLD = FAMILY_K / FDR_LEVEL
#: Trading days before a state other than "too early" (and before the
#: series' measured standard deviation replaces SIGMA_REF in the forecast).
MIN_DAYS_FOR_STATE = 63
#: Days the lag-1 autocorrelation is measured on, once (ADR 0018 §4, amended).
AR1_CHECK_DAYS = 63
AR1_FLAG = 0.15
#: Posterior prior scale, per 21 trading days, and the sensitivity strip.
TAU_21D = 0.005
TAU_GRID_21D = (0.0025, 0.005, 0.01, 0.02)
HORIZON_DAYS = 21
TRADING_DAYS_PER_YEAR = 252
WITHIN_YEARS = (5, 10, 20)
#: Most points returned for the e-value chart.
_MAX_PATH_POINTS = 260


@dataclass(frozen=True)
class Family:
    key: str
    series: str
    role: str  # "primary" | "secondary"
    question: str


FAMILIES = (
    Family("F1", SERIES_IDEAS, "primary", "Do Discover's picks, held as issued, beat your passive core ETF?"),
    Family("F2", SERIES_RANKING, "primary", "Does Discover's score sort winners from losers among every scored stock?"),
    Family("F3", SERIES_ADVISOR, "secondary", "Do the advisor's directional calls beat your passive core ETF?"),
)

#: Assumed edges for the "time to know" forecast. Ideas and advisor: mean
#: active return per 21 days with a 5 % basket noise. Ranking: a rank IC per
#: run with an IC standard deviation of 0.12 (the historical model's IC was
#: 0.07). These are planning numbers, not measurements.
EDGE_GRID_21D = (0.005, 0.01, 0.02)
EDGE_DEFAULT_21D = 0.01
BASKET_SD_21D = 0.05
IC_GRID = (0.02, 0.04, 0.07)
IC_DEFAULT = 0.07
IC_SD = 0.12


def _sharpe_daily(series: str, assumption: float, sd_daily: float) -> float:
    if series == SERIES_RANKING:
        return assumption / IC_SD / math.sqrt(HORIZON_DAYS)
    return (assumption / HORIZON_DAYS) / sd_daily


def _years(days: int | None) -> float | None:
    return None if days is None else days / TRADING_DAYS_PER_YEAR


def _forecast(series: str, sd_daily: float, measured_sd: bool) -> dict[str, Any]:
    within = tuple(y * TRADING_DAYS_PER_YEAR for y in WITHIN_YEARS)
    if series == SERIES_RANKING:
        grid, default, unit = IC_GRID, IC_DEFAULT, "rank_ic"
        sd = sd_daily
    else:
        grid, default, unit = EDGE_GRID_21D, EDGE_DEFAULT_21D, "excess_per_21d"
        # Without a measured series the basket noise sets the daily scale.
        sd = sd_daily if measured_sd else BASKET_SD_21D / math.sqrt(HORIZON_DAYS)
    # Rounded so the cached simulation is reused while the measured value drifts.
    sd = max(round(sd, 4), 0.0001)

    def run(assumption: float) -> fv.DaysToVerdict:
        return fv.days_to_verdict(
            round(_sharpe_daily(series, assumption, sd), 6), sd, CLIP, THRESHOLD,
            prior_var=PRIOR_VAR, cap_factor=BET_CAP, within=within,
        )

    main = run(default)
    return {
        "assumption_unit": unit,
        "assumption": default,
        "ic_sd": IC_SD if series == SERIES_RANKING else None,
        "basket_sd_21d": None if series == SERIES_RANKING or measured_sd else BASKET_SD_21D,
        "sd_daily": sd,
        "sd_measured": measured_sd,
        "q10_days": main.q10,
        "q50_days": main.q50,
        "q90_days": main.q90,
        "q10_years": _years(main.q10),
        "q50_years": _years(main.q50),
        "q90_years": _years(main.q90),
        "max_days": main.n_max,
        "p_within": [
            {"years": y, "p": main.p_within[y * TRADING_DAYS_PER_YEAR]} for y in WITHIN_YEARS
        ],
        "grid": [
            {"assumption": a, "q50_days": (r := run(a)).q50, "q50_years": _years(r.q50)} for a in grid
        ],
    }


def _posterior(values: list[float]) -> dict[str, Any] | None:
    """Posterior of the mean active return, in units per 21 trading days."""
    n = len(values)
    lag = fv.newey_west_lag(n)
    # Below MIN_DAYS_FOR_STATE the sample variance is too unstable to trust a
    # small one: the reference noise sets a floor on the standard error.
    floor = SIGMA_REF / math.sqrt(n) if 0 < n < MIN_DAYS_FOR_STATE else 0.0

    def post(tau_21d: float) -> fv.Posterior | None:
        return fv.normal_posterior(values, tau_21d / HORIZON_DAYS, lag, se_floor=floor)

    main = post(TAU_21D)
    if main is None:
        return None
    k = HORIZON_DAYS
    return {
        "tau_21d": TAU_21D,
        "n": n,
        "sample_mean_21d": main.sample_mean * k,
        "sample_se_21d": main.sample_se * k,
        "shrinkage": main.shrinkage,
        "mean_21d": main.mean * k,
        "ci_low_21d": main.ci_low * k,
        "ci_high_21d": main.ci_high * k,
        "p_positive": main.p_positive,
        "sensitivity": [
            {"tau_21d": tau, "p_positive": p.p_positive, "mean_21d": p.mean * k}
            for tau in TAU_GRID_21D
            if (p := post(tau)) is not None
        ],
    }


def _thin(days: list[date], skill: np.ndarray, harm: np.ndarray) -> list[dict[str, Any]]:
    n = len(days)
    if n == 0:
        return []
    step = max(1, math.ceil(n / _MAX_PATH_POINTS))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return [{"day": days[i].isoformat(), "e_skill": float(skill[i]), "e_harm": float(harm[i])} for i in idx]


def _state(n_days: int, skill: bool, harm: bool) -> str:
    if harm:
        return "harm"
    if skill:
        return "skill"
    return "too_early" if n_days < MIN_DAYS_FOR_STATE else "no_evidence"


def series_result(family: Family, rows: list[TrustDailyActiveReturn]) -> dict[str, Any]:
    """Test, posterior and forecast for one family from its frozen rows."""
    rows = sorted(rows, key=lambda r: r.day)
    pre = [r for r in rows if r.day < PRIMARY_TEST_START]
    live = [r for r in rows if r.day >= PRIMARY_TEST_START]
    observed = [r for r in live if r.value is not None and r.n_open > 0]
    values = [float(r.value) for r in observed if r.value is not None]
    days = [r.day for r in observed]
    n = len(values)

    y = (np.clip(np.asarray(values, dtype=float), -CLIP, CLIP) + CLIP) / (2.0 * CLIP)
    skill = fv.e_process_bernoulli(y, 0.5, PRIOR_VAR, cap_factor=BET_CAP)
    harm = fv.e_process_bernoulli(1.0 - y, 0.5, PRIOR_VAR, cap_factor=BET_CAP)
    rejected = fv.e_bh([skill.final, harm.final], FDR_LEVEL)
    state = _state(n, rejected[0], rejected[1])

    sd_measured = n >= MIN_DAYS_FOR_STATE
    sd = float(np.std(values, ddof=1)) if sd_measured else SIGMA_REF
    if not sd > 0:
        sd, sd_measured = SIGMA_REF, False
    ci = fv.mean_ci_hac(values, fv.newey_west_lag(n), 0.9) if values else None
    rho1 = fv.lag1_autocorrelation(values[:AR1_CHECK_DAYS]) if n >= AR1_CHECK_DAYS else None
    pre_values = [float(r.value) for r in pre if r.value is not None and r.n_open > 0]
    clipped = int(np.sum(np.abs(np.asarray(values)) > CLIP)) if values else 0
    return {
        "family": family.key,
        "series": family.series,
        "role": family.role,
        "question": family.question,
        "start": PRIMARY_TEST_START.isoformat(),
        "n_days": n,
        "n_empty_days": sum(1 for r in live if r.value is None or r.n_open == 0),
        "n_stale_days": sum(1 for r in observed if r.n_stale > 0),
        "n_clipped": clipped,
        "first_day": days[0].isoformat() if days else None,
        "last_day": live[-1].day.isoformat() if live else None,
        "mean_open": float(np.mean([r.n_open for r in observed])) if observed else None,
        "mean_21d": float(np.mean(values)) * HORIZON_DAYS if values else None,
        "mean_ci_21d": [ci[0] * HORIZON_DAYS, ci[1] * HORIZON_DAYS] if ci else None,
        "sd_daily": float(np.std(values, ddof=1)) if n >= 2 else None,
        "e_skill": skill.final,
        "e_harm": harm.final,
        "e_skill_peak": skill.max_value,
        "e_harm_peak": harm.max_value,
        "threshold": THRESHOLD,
        "n_tests": FAMILY_K,
        "state": state,
        "rho1": rho1,
        "rho1_flag": rho1 is not None and abs(rho1) > AR1_FLAG,
        "pre_registration": {
            "n_days": len(pre_values),
            "mean_21d": float(np.mean(pre_values)) * HORIZON_DAYS if pre_values else None,
        },
        "path": _thin(days, skill.path, harm.path),
        "posterior": _posterior(values),
        "time_to_know": _forecast(family.series, sd, sd_measured),
    }


def paired_comparison(
    ideas: list[TrustDailyActiveReturn], advisor: list[TrustDailyActiveReturn]
) -> dict[str, Any]:
    """Advisor against Discover on the days both had open calls (ADR 0018 Phase 3).

    Both series are active returns against the same passive core, so their
    daily difference is the advisor's return over Discover's picks with the
    benchmark cancelled. Descriptive only: no e-value, no threshold, no state.
    The advisor is a paper-only research loop and F1 stays the headline.
    """

    def observed(rows: list[TrustDailyActiveReturn]) -> dict[date, float]:
        return {r.day: float(r.value) for r in rows if r.value is not None and r.n_open > 0}

    a, b = observed(advisor), observed(ideas)
    common = sorted(d for d in a.keys() & b.keys() if d >= PRIMARY_TEST_START)
    n_pre = sum(1 for d in a.keys() & b.keys() if d < PRIMARY_TEST_START)
    diffs = [a[d] - b[d] for d in common]
    n = len(diffs)
    ci = fv.mean_ci_hac(diffs, fv.newey_west_lag(n), 0.9) if diffs else None
    cumulative = np.cumsum(diffs) if diffs else np.array([])
    step = max(1, math.ceil(n / _MAX_PATH_POINTS)) if n else 1
    idx = list(range(0, n, step))
    if n and idx[-1] != n - 1:
        idx.append(n - 1)
    return {
        "question": "Did the advisor's paper calls beat Discover's picks on the days both were open?",
        "start": PRIMARY_TEST_START.isoformat(),
        "n_days": n,
        "n_days_pre_registration": n_pre,
        "n_days_advisor_only": sum(1 for d in a if d >= PRIMARY_TEST_START and d not in b),
        "n_days_ideas_only": sum(1 for d in b if d >= PRIMARY_TEST_START and d not in a),
        "mean_21d": float(np.mean(diffs)) * HORIZON_DAYS if diffs else None,
        "mean_ci_21d": [ci[0] * HORIZON_DAYS, ci[1] * HORIZON_DAYS] if ci else None,
        "share_advisor_ahead": float(np.mean([x > 0 for x in diffs])) if diffs else None,
        "enough_days": n >= MIN_DAYS_FOR_STATE,
        "path": [{"day": common[i].isoformat(), "cumulative": float(cumulative[i])} for i in idx],
    }


def build_daily_tests(db: Session, user_id: str) -> dict[str, Any]:
    """Every family's result for one user, plus the fixed test settings."""
    rows = (
        db.query(TrustDailyActiveReturn)
        .filter(TrustDailyActiveReturn.user_id == user_id, TrustDailyActiveReturn.series.in_(SERIES))
        .all()
    )
    by_series: dict[str, list[TrustDailyActiveReturn]] = {s: [] for s in SERIES}
    for r in rows:
        by_series[r.series].append(r)
    return {
        "start": PRIMARY_TEST_START.isoformat(),
        "clip": CLIP,
        "sigma_ref": SIGMA_REF,
        "prior_var": PRIOR_VAR,
        "bet_cap": BET_CAP,
        "fdr_level": FDR_LEVEL,
        "threshold": THRESHOLD,
        "min_days_for_state": MIN_DAYS_FOR_STATE,
        "horizon_days": HORIZON_DAYS,
        "families": [series_result(f, by_series[f.series]) for f in FAMILIES],
        "paired": paired_comparison(by_series[SERIES_IDEAS], by_series[SERIES_ADVISOR]),
    }


__all__ = [
    "CLIP",
    "FAMILIES",
    "MIN_DAYS_FOR_STATE",
    "PRIMARY_TEST_START",
    "THRESHOLD",
    "build_daily_tests",
    "paired_comparison",
    "series_result",
]
