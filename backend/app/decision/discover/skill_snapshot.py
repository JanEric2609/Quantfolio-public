"""Discovery skill snapshot aggregation (P1).

Aggregates a batch of scored ``DiscoveryPrediction`` rows into a single
``DiscoverySkillSnapshot`` row so the Discover UI can trend forecasting skill
over time.
"""
from __future__ import annotations

import logging
from datetime import date
from statistics import mean

import numpy as np
from sqlalchemy.orm import Session

from app.decision.discover.resolution import benchmark_symbol
from app.decision.discover.scoring import (
    DEFAULT_HORIZON_CALENDAR_DAYS,
    MIN_IC_NAMES,
    MIN_INDEPENDENT_WINDOWS,
    hit_rate_interval,
    ic_summary,
    independent_windows,
    outcome_return,
    rank_ic_by_date,
)
from app.foundation.models.entities import DiscoveryPrediction, DiscoverySkillSnapshot
from app.foundation.models.entities._core import now_utc

logger = logging.getLogger(__name__)


def write_skill_snapshot(
    db: Session, resolved: list[dict]
) -> DiscoverySkillSnapshot | None:
    """Aggregate a resolved batch into one ``DiscoverySkillSnapshot`` row.

    Args:
        db: Active SQLAlchemy session.
        resolved: Result list from ``resolve_due_predictions``.

    Only Discover's own predictions count: the advisor's rows (with a
    ``portfolio_id``) are scored per sleeve by the advisor scorecard.

    Returns:
        The created snapshot row, or ``None`` if *resolved* holds no Discover
        prediction.
    """
    prediction_ids = [r["prediction_id"] for r in resolved]
    rows = {
        row.id: row
        for row in db.query(DiscoveryPrediction)
        .filter(DiscoveryPrediction.id.in_(prediction_ids))
        .all()
    } if resolved else {}
    resolved = [
        r for r in resolved
        if rows.get(r["prediction_id"]) is not None and rows[r["prediction_id"]].portfolio_id is None
    ]
    if not resolved:
        logger.info("skill_snapshot: no resolved Discover predictions, skipping snapshot")
        return None

    first_pred = rows.get(resolved[0]["prediction_id"])
    user_id = first_pred.user_id if first_pred else None

    total_predictions = len(resolved)
    total_resolved = sum(1 for r in resolved if r.get("outcome_status") == "resolved")

    # Metrics are computed from resolved rows that have been scored.
    scored_rows = [
        rows[r["prediction_id"]]
        for r in resolved
        if r.get("outcome_status") == "resolved" and rows.get(r["prediction_id"]) is not None
    ]
    score_jsons = [r.score_json or {} for r in scored_rows]

    hits = [float(sj["hit"]) for sj in score_jsons if sj.get("hit") is not None]
    briers = [float(sj["brier"]) for sj in score_jsons if sj.get("brier") is not None]

    hit_rate = _mean(hits)
    brier_score_avg = _mean(briers)

    mincer_a0 = _first_metric(score_jsons, "mincer_a0")
    mincer_a1 = _first_metric(score_jsons, "mincer_a1")

    # Rank IC is per prediction date; the snapshot carries the mean across the
    # dates in this batch (usually one) and the per-date values, so a trend
    # can be built over snapshots without mixing weeks.
    valid = [
        r for r in resolved
        if r.get("outcome_status") == "resolved"
        and r.get("conviction") is not None
        and outcome_return(r) is not None
    ]
    by_date = rank_ic_by_date(valid)
    summary = ic_summary(by_date)
    excess = [r["excess_return"] for r in valid if r.get("excess_return") is not None]
    details = {
        "ic_by_date": {day: {"ic": ic, "n": n} for day, (ic, n) in sorted(by_date.items())},
        "ic_t_stat": summary["t_stat"],
        "hit_basis": "excess" if excess and len(excess) == len(valid) else "mixed" if excess else "raw",
        "mean_excess_return": _mean(excess),
        "benchmark": _benchmark(rows.values()),
    }
    ece = _compute_ece(resolved, rows)

    snapshot = DiscoverySkillSnapshot(
        user_id=user_id,
        snapshot_at=now_utc(),
        metric_type="batch",
        total_predictions=total_predictions,
        total_resolved=total_resolved,
        hit_rate=hit_rate,
        brier_score_avg=brier_score_avg,
        rank_ic=summary["mean_ic"],
        icir=summary["icir"],
        ece=ece,
        mincer_a0=mincer_a0,
        mincer_a1=mincer_a1,
        details_json=details,
    )
    db.add(snapshot)

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "skill_snapshot: wrote snapshot user=%s total=%d resolved=%d",
        user_id,
        total_predictions,
        total_resolved,
    )
    return snapshot


def _mean(values: list[float | int]) -> float | None:
    return float(mean(values)) if values else None


def _benchmark(rows) -> str | None:
    for row in rows:
        name = ((row.score_json or {}).get("outcome") or {}).get("benchmark")
        if name:
            return str(name)
    return None


def _first_metric(score_jsons: list[dict], key: str) -> float | None:
    for sj in score_jsons:
        value = sj.get(key)
        if value is not None:
            return float(value)
    return None


def _compute_ece(
    resolved: list[dict], rows: dict[str, DiscoveryPrediction], bins: int = 10
) -> float | None:
    """Expected Calibration Error binned by conviction deciles."""
    eligible = [
        (rows[r["prediction_id"]].conviction, (rows[r["prediction_id"]].score_json or {}).get("hit"))
        for r in resolved
        if r.get("outcome_status") == "resolved"
        and rows.get(r["prediction_id"]) is not None
        and rows[r["prediction_id"]].conviction is not None
        and (rows[r["prediction_id"]].score_json or {}).get("hit") is not None
    ]

    if len(eligible) < bins:
        return None

    convictions = np.array([c for c, _ in eligible])
    hits = np.array([h for _, h in eligible], dtype=float)
    n = len(eligible)

    # Assign each observation to a decile by sorted conviction position.
    order = np.argsort(convictions, kind="stable")
    assigned = np.empty(n, dtype=int)
    assigned[order] = np.minimum(np.floor(np.arange(n) / (n / bins)).astype(int), bins - 1)

    ece = 0.0
    for b in range(bins):
        mask = assigned == b
        if not np.any(mask):
            continue
        avg_conf = float(np.mean(convictions[mask]))
        avg_hit = float(np.mean(hits[mask]))
        ece += abs(avg_conf - avg_hit) * float(np.sum(mask)) / n

    return ece


def skill_summary(db: Session, user_id: str) -> dict:
    """Skill of Discover's resolved predictions for *user_id*, judged per date.

    Rank IC is computed within each prediction date and then averaged, with
    its ICIR and an overlap-aware t-statistic across dates; the hit rate
    counts picks that beat the passive core over their own window, with a
    95 % interval clustered by date. ``independent_windows`` says how many
    non-overlapping horizons the record spans; neither the t nor the
    interval means much before ``min_independent_windows``. Also reports what
    is still pending, so an empty track record says when the first results
    land. The advisor's rows are left out (see ``write_skill_snapshot``).
    """
    preds = (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.portfolio_id.is_(None),
            DiscoveryPrediction.outcome_status.in_(("resolved", "pending")),
        )
        .all()
    )
    pending = [p for p in preds if p.outcome_status == "pending"]
    rows = [
        {
            "conviction": p.conviction,
            "realised_return": p.realised_return,
            "excess_return": p.excess_return,
            "predicted_on": p.predicted_at.date().isoformat(),
        }
        for p in preds
        if p.outcome_status == "resolved" and p.realised_return is not None
    ]
    judged = [outcome_return(r) for r in rows]
    judged = [x for x in judged if x is not None]
    excess = [r["excess_return"] for r in rows if r["excess_return"] is not None]
    summary = ic_summary(
        rank_ic_by_date([r for r in rows if r["conviction"] is not None])
    )
    judged_days = [
        (date.fromisoformat(r["predicted_on"]), x > 0)
        for r in rows
        if (x := outcome_return(r)) is not None
    ]
    next_resolve = min((p.resolve_at for p in pending), default=None)
    return {
        "benchmark": benchmark_symbol(db),
        "resolved": len(rows),
        "resolved_vs_benchmark": len(excess),
        "pending": len(pending),
        "next_resolve_at": next_resolve.isoformat() if next_resolve else None,
        "hit_rate": _mean([1.0 if x > 0 else 0.0 for x in judged]),
        "mean_excess_return": _mean(excess),
        "mean_rank_ic": summary["mean_ic"],
        "icir": summary["icir"],
        "ic_t_stat": summary["t_stat"],
        "ic_dates": summary["dates"],
        "min_ic_names": MIN_IC_NAMES,
        "ic_nw_lags": summary["nw_lags"],
        "hit_rate_ci_half_width": hit_rate_interval(judged_days),
        "independent_windows": independent_windows([d for d, _ in judged_days]),
        "min_independent_windows": MIN_INDEPENDENT_WINDOWS,
        "horizon_calendar_days": DEFAULT_HORIZON_CALENDAR_DAYS,
    }
