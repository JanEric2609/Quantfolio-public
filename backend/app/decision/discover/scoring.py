"""Discovery prediction scoring (P1, reworked in Phase 2).

Computes per-prediction and batch-level scoring metrics for resolved
``DiscoveryPrediction`` rows and persists them in ``score_json``.

Skill is measured against the passive core, not against zero: the outcome of a
prediction is its ``excess_return`` (see ``resolution``), and a hit means the
pick beat the ETF over the same window. In a rising market "went up" is true
of almost every long pick and says nothing about skill. Rows resolved before
the benchmark was recorded fall back to the raw return.

Rank IC is cross-sectional, so it is computed per prediction date: mixing
predictions from different weeks into one correlation mostly measures which
week the market went up. A date needs ``MIN_IC_NAMES`` scored names before its
IC counts; with fewer, a rank correlation is noise.

Two sources write to the ledger: Discover's weekly shortlist (``portfolio_id``
NULL, conviction is the composite score) and the advisor's daily cycle (one
row per ticker and paper sleeve, conviction is the LLM's confidence). Their
scores are on different scales, so a cross-section never mixes them: advisor
rows are grouped per sleeve and date. A logged hold ("neutral") claims nothing
about beating the ETF and is left out of the IC.

Dates are not independent evidence. Every prediction is judged over the same
21-trading-day horizon, so weekly Discover dates overlap about four deep and
the advisor's daily dates about twenty deep. The t-statistic is therefore
Newey-West with as many lags as dates fall inside one horizon, and the
sample is also counted as ``independent_windows``: dates at least one horizon
apart. A simulation with no skill at all (docs/archive/audits/2026-09-24-why-it-does-
not-work §9, repro/04) puts a plain t above 2 in 13-16 % of weekly and
28-38 % of daily records, against the 2.5 % a one-sided t > 2 allows.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date
from typing import Any, cast

import numpy as np
import scipy.stats
from sklearn.linear_model import LogisticRegression
from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryPrediction
from app.foundation.quant_metrics import newey_west_t_stat

logger = logging.getLogger(__name__)

MIN_IC_NAMES = 5
# 21 trading days (ledger.DEFAULT_HORIZON_DAYS) span about 29 calendar days.
DEFAULT_HORIZON_CALENDAR_DAYS = 29
# How many non-overlapping horizons a record needs before its IC t-statistic
# or hit-rate interval is shown: a year of monthly windows.
MIN_INDEPENDENT_WINDOWS = 12


def outcome_return(row: dict[str, Any]) -> float | None:
    """The return a prediction is judged on: excess over the benchmark if known."""
    excess = row.get("excess_return")
    return excess if excess is not None else row.get("realised_return")


def score_batch(db: Session, resolved: list[dict]) -> None:
    """Score a batch of resolved predictions.

    Computes a hit flag (beat the benchmark), a Brier score, the rank IC of
    the prediction's date, and a logistic hit-probability fit for every row,
    then merges them into ``score_json`` next to the resolution's ``outcome``.
    Failures for a single row are logged and skipped.

    Args:
        db: Active SQLAlchemy session.
        resolved: Result list from ``resolve_due_predictions``.
    """
    if not resolved:
        return

    valid_rows = [
        r for r in resolved if r.get("conviction") is not None and outcome_return(r) is not None
    ]
    ic_by_date = rank_ic_by_date(valid_rows)
    mincer_a0, mincer_a1 = _fit_hit_probability_logit(valid_rows)

    prediction_ids = [r["prediction_id"] for r in resolved]
    rows = {
        row.id: row
        for row in db.query(DiscoveryPrediction)
        .filter(DiscoveryPrediction.id.in_(prediction_ids))
        .all()
    }

    for item in resolved:
        try:
            pred = rows.get(item["prediction_id"])
            if pred is None:
                logger.warning(
                    "scoring: prediction %s not found in DB, skipping", item["prediction_id"]
                )
                continue

            outcome = outcome_return(item)
            hit = 1 if (outcome is not None and outcome > 0) else 0
            # The raw conviction is a composite score, not a probability; the
            # Brier score is only meaningful against the calibrated value.
            calibrated = item.get("conviction_calibrated")
            probability = calibrated if calibrated is not None else item.get("conviction")
            brier = (probability - hit) ** 2 if probability is not None else None
            # A hold is left out of its cross-section, so it carries no IC.
            date_ic = None if item.get("direction") == "neutral" else ic_by_date.get(ic_group(item))

            pred.score_json = {
                **(pred.score_json or {}),
                "hit": hit,
                "hit_basis": "excess" if item.get("excess_return") is not None else "raw",
                "brier": brier,
                "brier_basis": "calibrated" if calibrated is not None else "raw_score",
                # IC of this prediction's cross-section (its date, and its
                # sleeve for advisor rows); None when it had fewer than
                # MIN_IC_NAMES directional names.
                "rank_ic": date_ic[0] if date_ic else None,
                "rank_ic_n": date_ic[1] if date_ic else None,
                # Historically named "mincer_*" (matching DiscoverySkillSnapshot's
                # mincer_a0/mincer_a1 DB columns, which mirror this JSON key
                # naming) — actually the intercept/coefficient of a logistic
                # regression of hit on conviction, i.e. Platt scaling, not a
                # Mincer-Zarnowitz regression (F14). Left as a documented naming
                # quirk rather than a live-schema rename; see
                # _fit_hit_probability_logit and
                # app.foundation.quant_metrics.compute_mincer_zarnowitz for the
                # real MZ regression.
                "mincer_a0": mincer_a0,
                "mincer_a1": mincer_a1,
            }
        except Exception as exc:  # pragma: no cover - defensive batch safety
            logger.warning(
                "scoring: failed to score prediction %s: %s", item.get("prediction_id"), exc
            )
            continue

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info("scoring: scored %d predictions", len(resolved))


def ic_group(row: dict[str, Any]) -> str:
    """The cross-section a row is ranked in: its date, and its sleeve if any.

    Discover rows key by ``predicted_on`` alone; advisor rows append their
    paper portfolio so each sleeve's daily call is its own cross-section.
    """
    day = row.get("predicted_on") or ""
    portfolio = row.get("portfolio_id")
    return f"{day}|{portfolio}" if portfolio else day


def rank_ic_by_date(valid_rows: list[dict[str, Any]]) -> dict[str, tuple[float, int]]:
    """Spearman IC of conviction vs outcome for each cross-section.

    Returns ``{ic_group: (ic, n)}`` (the date for Discover rows) for groups
    with at least ``MIN_IC_NAMES`` directional rows and a defined correlation.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in valid_rows:
        if r.get("direction") == "neutral":
            continue
        groups[ic_group(r)].append(r)
    out: dict[str, tuple[float, int]] = {}
    for day, rows in groups.items():
        if len(rows) < MIN_IC_NAMES:
            continue
        ic = _compute_rank_ic(rows)
        if ic is not None:
            out[day] = (ic, len(rows))
    return out


def _day(key: str) -> date | None:
    try:
        return date.fromisoformat(key[:10])
    except ValueError:
        return None


def independent_windows(days: list[date], horizon_days: int = DEFAULT_HORIZON_CALENDAR_DAYS) -> int:
    """How many of *days* are at least one horizon apart (greedy, oldest first)."""
    count, last = 0, None
    for day in sorted(set(days)):
        if last is None or (day - last).days >= horizon_days:
            count, last = count + 1, day
    return count


def overlap_lags(days: list[date], horizon_days: int = DEFAULT_HORIZON_CALENDAR_DAYS) -> int:
    """Newey-West lags for dates spaced like *days*: later dates inside one horizon."""
    ordered = sorted(set(days))
    if len(ordered) < 2:
        return 0
    gap = float(np.median([(b - a).days for a, b in zip(ordered, ordered[1:], strict=False)]))
    return max(0, math.ceil(horizon_days / max(gap, 1.0)) - 1)


def ic_summary(
    ic_by_date: dict[str, tuple[float, int]],
    horizon_days: int = DEFAULT_HORIZON_CALENDAR_DAYS,
) -> dict[str, float | int | None]:
    """Mean IC across dates, its ICIR (mean / sd), and an overlap-aware t.

    ``t_stat`` is Newey-West over the date-ordered ICs with ``nw_lags`` from
    the date spacing (a plain t when the dates do not overlap);
    ``independent_windows`` counts dates at least one horizon apart.
    """
    ordered = sorted(ic_by_date.items(), key=lambda kv: kv[0])
    ics = [ic for _, (ic, _) in ordered]
    days = [d for k, _ in ordered if (d := _day(k)) is not None]
    windows = independent_windows(days, horizon_days)
    if not ics:
        return {"mean_ic": None, "icir": None, "t_stat": None, "dates": 0, "nw_lags": 0, "independent_windows": 0}
    mean_ic = float(np.mean(ics))
    if len(ics) < 2:
        return {"mean_ic": mean_ic, "icir": None, "t_stat": None, "dates": 1, "nw_lags": 0, "independent_windows": windows}
    sd = float(np.std(ics, ddof=1))
    icir = mean_ic / sd if sd > 0 else None
    lags = overlap_lags(days, horizon_days)
    if icir is None:
        t_stat = None
    elif lags == 0 or len(ics) < 3:
        t_stat = icir * math.sqrt(len(ics))
    else:
        t_stat = float(newey_west_t_stat(np.array(ics), lags))
    return {
        "mean_ic": mean_ic, "icir": icir, "t_stat": t_stat, "dates": len(ics),
        "nw_lags": lags, "independent_windows": windows,
    }


def hit_rate_interval(
    rows: list[tuple[date, bool]],
    horizon_days: int = DEFAULT_HORIZON_CALENDAR_DAYS,
) -> float | None:
    """Half-width of a 95 % interval for the hit rate, clustered by date.

    Calls made on one date share one market window, and nearby dates share
    most of it, so they are not independent coin flips. Each date's surplus
    of hits over the overall rate is one observation, with Newey-West
    weights across dates inside one horizon. None until the record spans
    ``MIN_INDEPENDENT_WINDOWS`` horizons: with fewer clusters the interval
    itself is unreliable.
    """
    if not rows:
        return None
    days = [d for d, _ in rows]
    if independent_windows(days, horizon_days) < MIN_INDEPENDENT_WINDOWS:
        return None
    rate = sum(hit for _, hit in rows) / len(rows)
    surplus: dict[date, float] = defaultdict(float)
    for day, hit in rows:
        surplus[day] += float(hit) - rate
    u = np.array([surplus[d] for d in sorted(surplus)])
    lags = overlap_lags(days, horizon_days)
    long_run = float((u ** 2).sum())
    for lag in range(1, min(lags, len(u) - 1) + 1):
        long_run += 2.0 * (1.0 - lag / (lags + 1.0)) * float((u[lag:] * u[:-lag]).sum())
    return 1.96 * math.sqrt(max(long_run, 0.0)) / len(rows)


def _compute_rank_ic(valid_rows: list[dict[str, Any]]) -> float | None:
    """Return Spearman rank correlation between conviction and outcome."""
    if len(valid_rows) < 2:
        return None

    convictions = [r["conviction"] for r in valid_rows]
    returns = [outcome_return(r) for r in valid_rows]

    spearman_result = cast(Any, scipy.stats.spearmanr(convictions, returns))
    rank_ic = float(
        spearman_result.statistic
        if hasattr(spearman_result, "statistic")
        else spearman_result[0]
    )
    if math.isnan(rank_ic):
        return None
    return rank_ic


def _fit_hit_probability_logit(valid_rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Fit a logistic regression of hit (binary) on conviction — Platt scaling,
    not a Mincer-Zarnowitz regression (F14: this was previously named/called
    "Mincer" despite having no magnitude channel — the outcome only enters as
    its sign). A hit is ``outcome_return > 0``: excess over the benchmark when
    the row has one. Returns (intercept, coefficient); see
    app.foundation.quant_metrics.compute_mincer_zarnowitz for the actual
    MZ regression (``realised = a + b * predicted``, used against a real
    magnitude forecast)."""
    n = len(valid_rows)
    if n == 0:
        return None, None

    convictions = np.array([r["conviction"] for r in valid_rows])
    hit = np.array([1 if (outcome_return(r) or 0.0) > 0 else 0 for r in valid_rows])

    if n == 1:
        return 0.0, 1.0

    if len(np.unique(hit)) < 2:
        return None, None

    try:
        model = LogisticRegression()
        model.fit(convictions.reshape(-1, 1), hit)
        return float(cast(Any, model.intercept_)[0]), float(cast(Any, model.coef_)[0, 0])
    except Exception:
        return None, None
