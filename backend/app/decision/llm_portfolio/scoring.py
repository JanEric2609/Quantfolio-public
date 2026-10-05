"""Outcome scoring for weekly mandate-review decisions.

Each completed review carries a structured, measurable ``expectation``:
``{metric, direction, magnitude, horizon_weeks, confidence}``. Once
``horizon_weeks`` elapses, this job computes the actual metric deterministically
and stamps a ``verdict`` — the LLM never grades its own homework.

Verdict semantics (comparing the measured metric against the stated direction):
  * ``hit``          — the metric moved in the stated direction and met the magnitude
  * ``partial``      — moved in the right direction but short of the magnitude
  * ``miss``         — did not move in the stated direction
  * ``unresolvable`` — insufficient snapshot / price data to measure

Modelled on ``services/recommendation_outcomes.py``; benchmark is **EUNL.DE**.

**Documented limitation:** the expectation is scored at the *portfolio* level over
``[review_date, review_date + horizon_weeks]``. Because weekly reviews with
multi-week horizons overlap, this measures *"did the stated expectation hold"*,
not *"did this one trade cause it."* Accepted by design — the review is
assessment-led.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.foundation.models.entities import LlmPortfolioDecision, PaperSnapshot
from app.decision.llm_portfolio.assessment import BENCHMARK_TICKER, _fetch_interval_return

logger = logging.getLogger(__name__)

_VALID_METRICS = {"abs_return_pct", "benchmark_excess_pct", "max_drawdown_pct"}


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _portfolio_interval_return_pct(
    db: Session, portfolio_id: str, start: datetime, end: datetime
) -> Optional[float]:
    """Portfolio return over ``[start, end]`` as a percentage, from snapshots.

    Derived from since-inception ``total_return_pct`` on the snapshot nearest each
    endpoint: ``((1 + r_end/100) / (1 + r_start/100) - 1) * 100``.
    """
    start_snap = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id, PaperSnapshot.date <= start.date())
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    end_snap = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id, PaperSnapshot.date <= end.date())
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    if start_snap is None or end_snap is None or start_snap.date == end_snap.date:
        return None
    r_start = _to_float(start_snap.total_return_pct)
    r_end = _to_float(end_snap.total_return_pct)
    if r_start is None or r_end is None:
        return None
    return (((1 + r_end / 100) / (1 + r_start / 100)) - 1.0) * 100


def _worst_drawdown_pct(
    db: Session, portfolio_id: str, start: datetime, end: datetime
) -> Optional[float]:
    """Most negative ``max_drawdown`` among snapshots in the window, as a percentage."""
    snaps = (
        db.query(PaperSnapshot)
        .filter(
            PaperSnapshot.portfolio_id == portfolio_id,
            PaperSnapshot.date >= start.date(),
            PaperSnapshot.date <= end.date(),
            PaperSnapshot.max_drawdown.isnot(None),
        )
        .all()
    )
    values = [_to_float(s.max_drawdown) for s in snaps]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return min(values) * 100


def compute_actual_metric(
    db: Session,
    portfolio_id: str,
    metric: str,
    start: datetime,
    end: datetime,
) -> Optional[float]:
    """Measure the actual value of ``metric`` (as a percentage) over the window."""
    if metric == "abs_return_pct":
        return _portfolio_interval_return_pct(db, portfolio_id, start, end)
    if metric == "benchmark_excess_pct":
        port = _portfolio_interval_return_pct(db, portfolio_id, start, end)
        if port is None:
            return None
        bench = _fetch_interval_return(db, BENCHMARK_TICKER, start, end)
        if bench is None:
            return None
        return port - bench * 100
    if metric == "max_drawdown_pct":
        return _worst_drawdown_pct(db, portfolio_id, start, end)
    return None


def score_expectation(actual_pct: float, expectation: dict[str, Any]) -> str:
    """Compare a measured metric to the stated expectation → verdict.

    Pure. ``direction`` folds ``magnitude`` into a signed target so return-up and
    drawdown-down are judged with one rule.
    """
    direction = str(expectation.get("direction", "")).lower()
    magnitude = abs(_to_float(expectation.get("magnitude")) or 0.0)

    if direction == "increase":
        if actual_pct >= magnitude:
            return "hit"
        return "partial" if actual_pct > 0 else "miss"
    if direction == "decrease":
        if actual_pct <= -magnitude:
            return "hit"
        return "partial" if actual_pct < 0 else "miss"
    # Unknown direction — can't score meaningfully.
    return "unresolvable"


def score_decision(db: Session, decision: LlmPortfolioDecision, now: datetime) -> Optional[str]:
    """Score one decision in place (does not commit). Returns the verdict or None.

    None means "not scoreable yet / skip" — horizon not elapsed, no expectation,
    or an unmeasurable metric.
    """
    if decision.horizon_weeks is None or decision.review_date is None:
        return None
    # SQLite returns naive datetimes even from tz-aware columns; treat as UTC so
    # the comparison against an aware `now` doesn't raise.
    review_date = decision.review_date
    if review_date.tzinfo is None:
        review_date = review_date.replace(tzinfo=UTC)
    end = review_date + timedelta(weeks=decision.horizon_weeks)
    if end > now:
        return None

    try:
        payload = json.loads(decision.decision_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    expectation = payload.get("expectation")
    if not isinstance(expectation, dict):
        return None
    metric = str(expectation.get("metric", ""))
    if metric not in _VALID_METRICS:
        return None

    actual = compute_actual_metric(db, decision.portfolio_id, metric, review_date, end)
    if actual is None:
        verdict = "unresolvable"
        actual_outcome: dict[str, Any] = {
            "metric": metric,
            "actual_pct": None,
            "verdict": verdict,
            "window": [review_date.isoformat(), end.isoformat()],
            "reason": "Insufficient snapshot / price data",
        }
    else:
        actual = round(actual, 2)
        verdict = score_expectation(actual, expectation)
        actual_outcome = {
            "metric": metric,
            "actual_pct": actual,
            "verdict": verdict,
            "expectation": expectation,
            "window": [review_date.isoformat(), end.isoformat()],
        }

    # Merge into reflection_json, preserving the existing tool/gate context.
    try:
        reflection = json.loads(decision.reflection_json or "{}") if decision.reflection_json else {}
    except json.JSONDecodeError:
        reflection = {}
    reflection["actual_outcome"] = actual_outcome

    decision.verdict = verdict
    decision.scored_at = now
    decision.reflection_json = json.dumps(reflection)
    return verdict


def run_scoring(db: Session | None = None) -> int:
    """Score all completed decisions whose horizon has elapsed and verdict is unset.

    Returns the count of decisions scored.
    """
    if db is None:
        from app.foundation.core.db import SessionLocal

        with SessionLocal() as own_db:
            return _run_scoring(own_db)
    return _run_scoring(db)


def get_decision_verdicts(
    db: Session, mandate: str, tickers: list[str] | None = None
) -> list[dict[str, Any]]:
    """Read-only: scored (verdict is not null) decisions for *mandate*.

    Filtered to *tickers* when given — matched against each decision's own
    ticker inside ``decision_json`` (there's no ticker column on the row).
    Never mutates; for read-only consumers like recommendation_engine's
    track-record evidence source.
    """
    ticker_filter = {t.upper() for t in tickers} if tickers else None
    rows = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == mandate, LlmPortfolioDecision.verdict.isnot(None))
        .order_by(LlmPortfolioDecision.scored_at.desc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            decision = json.loads(row.decision_json or "{}")
        except json.JSONDecodeError:
            decision = {}
        ticker = str(decision.get("ticker", "")).upper()
        if ticker_filter is not None and ticker not in ticker_filter:
            continue
        out.append({
            "ticker": ticker,
            "mandate": row.mandate,
            "verdict": row.verdict,
            "scored_at": row.scored_at.isoformat() if row.scored_at else None,
            "review_date": row.review_date.isoformat() if row.review_date else None,
        })
    return out


def _run_scoring(db: Session) -> int:
    now = datetime.now(UTC)
    pending = (
        db.query(LlmPortfolioDecision)
        .filter(
            LlmPortfolioDecision.status == "completed",
            LlmPortfolioDecision.verdict.is_(None),
            LlmPortfolioDecision.horizon_weeks.isnot(None),
        )
        .all()
    )
    scored = 0
    for decision in pending:
        try:
            verdict = score_decision(db, decision, now)
        except Exception as exc:
            logger.warning("Scoring failed for decision %s: %s", decision.id, exc)
            continue
        if verdict is not None:
            scored += 1
    db.commit()
    return scored
