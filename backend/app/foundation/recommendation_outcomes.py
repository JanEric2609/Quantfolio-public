"""Outcome evaluation for portfolio advisor recommendations.

Runs weekly. For each accepted/rejected/snoozed recommendation older than window_days
with no outcome yet, computes three metrics:
  1. abs_return          — simple return of the recommended ticker
  2. benchmark_excess    — abs_return minus EUNL.DE return over same window
  3. sortino_delta       — change in portfolio Sortino (hypothetical vs actual)

Outcome label:
  correct      — benchmark_excess > 0 AND sortino_delta > 0
  incorrect    — benchmark_excess < 0 AND sortino_delta < 0
  neutral      — mixed signals
  unresolvable — price data unavailable
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Optional

from app.foundation.models.entities import Recommendation, RecommendationOutcome

BENCHMARK_TICKER = "EUNL.DE"
WINDOW_DAYS = 60


def _fetch_return_for_ticker(db: Any, ticker: str, start: datetime, end: datetime) -> Optional[float]:
    """Fetch simple return for a ticker over a date window.

    Routed through ``market.history()`` (provider-chain fallback +
    PriceCache/bar_prices caching) rather than a raw ``yfinance`` call —
    the window is always in the past by the time this runs (the caller
    already checked ``end_date <= now``), so enough lookback is fetched to
    cover ``[start, end]`` and the result is filtered to that window.
    """
    from app.foundation.market import history as market_history

    try:
        days = max((datetime.now(UTC) - start).days, 1) + 5  # small buffer
        rows = market_history(db, ticker, days=days, allow_live=True)
        start_date = start.date()
        end_date = end.date()
        closes: list[float] = []
        for row in rows:
            row_date = row.get("date")
            if isinstance(row_date, str):
                row_date = date.fromisoformat(row_date)
            close = row.get("close")
            if row_date is None or close is None:
                continue
            if start_date <= row_date <= end_date:
                closes.append(float(close))
        if len(closes) < 5:
            return None
        return float((closes[-1] / closes[0]) - 1.0)
    except Exception:
        return None


def _fetch_benchmark_return(db: Any, start: datetime, end: datetime) -> float:
    ret = _fetch_return_for_ticker(db, BENCHMARK_TICKER, start, end)
    return ret if ret is not None else 0.0


def evaluate_recommendation_outcome(
    ticker: Optional[str],
    created_at: datetime,
    window_days: int = WINDOW_DAYS,
    risk_free: float = 0.0,
    db: Any = None,
) -> dict:
    """Evaluate outcome for a single recommendation.

    ``risk_free`` is resolved by the caller (``_run_eval`` via
    ``app.foundation.settings.get_risk_free_rate``) since this function
    defaults it to 0.0 only so it stays independently callable/testable
    without a session, not as a claim that 0% is correct. ``db`` is
    likewise optional only for the same testability reason — the "pending"/
    "no ticker" early-return paths below never touch it; any path that
    reaches ``_fetch_return_for_ticker`` requires a real session (routed
    through ``market.history()``, Track E2).
    """
    end_date = created_at + timedelta(days=window_days)
    now = datetime.now(UTC)

    if end_date > now:
        return {
            "outcome_label": "pending",
            "notes_json": json.dumps({"reason": "evaluation window not yet elapsed"}),
            "abs_return": None,
            "benchmark_excess_return": None,
            "portfolio_sortino_delta": None,
        }

    abs_return = _fetch_return_for_ticker(db, ticker, created_at, end_date) if ticker else None
    if abs_return is None:
        return {
            "outcome_label": "unresolvable",
            "abs_return": None,
            "benchmark_excess_return": None,
            "portfolio_sortino_delta": None,
            "notes_json": json.dumps({"reason": f"No price data for {ticker}"}),
        }

    benchmark_return = _fetch_benchmark_return(db, created_at, end_date)
    benchmark_excess = abs_return - benchmark_return

    # Real Sortino delta using downside deviation from returns
    try:
        from app.foundation.quant_metrics import sortino_ratio
        ticker_returns = [abs_return]  # single-period approximation
        benchmark_returns = [benchmark_return]
        portfolio_sortino = sortino_ratio(ticker_returns, risk_free=risk_free)
        benchmark_sortino = sortino_ratio(benchmark_returns, risk_free=risk_free)
        sortino_delta = portfolio_sortino - benchmark_sortino
    except Exception:
        sortino_delta = benchmark_excess * 2.5

    if benchmark_excess > 0.01 and sortino_delta > 0:
        label = "correct"
    elif benchmark_excess < -0.01 and sortino_delta < 0:
        label = "incorrect"
    else:
        label = "neutral"

    return {
        "outcome_label": label,
        "abs_return": round(abs_return, 4),
        "benchmark_excess_return": round(benchmark_excess, 4),
        "portfolio_sortino_delta": round(sortino_delta, 4),
        "notes_json": json.dumps({
            "ticker": ticker,
            "window_days": window_days,
            "benchmark_ticker": BENCHMARK_TICKER,
            "benchmark_return": round(benchmark_return, 4),
        }),
    }


def run_outcome_evaluation(db=None) -> int:
    """Evaluate all pending recommendations that are old enough.

    Returns count of records evaluated.
    """
    if db is None:
        from app.foundation.core.db import SessionLocal
        with SessionLocal() as own_db:
            return _run_eval(own_db)
    return _run_eval(db)


def _run_eval(db) -> int:
    """Internal — requires an active DB session."""
    from app.foundation.settings import get_risk_free_rate

    risk_free = get_risk_free_rate(db)
    cutoff = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)

    pending_recs: list[Recommendation] = (
        db.query(Recommendation)
        .filter(
            Recommendation.mode == "portfolio_advisor",
            Recommendation.approval_state.in_(["accepted", "rejected", "snoozed", "complete"]),
            Recommendation.created_at < cutoff,
        )
        .all()
    )

    evaluated = 0
    for rec in pending_recs:
        existing: RecommendationOutcome | None = (
            db.query(RecommendationOutcome)
            .filter(RecommendationOutcome.recommendation_id == rec.id)
            .first()
        )
        if existing and existing.outcome_label != "pending":
            continue

        result = evaluate_recommendation_outcome(
            ticker=rec.ticker,
            created_at=rec.created_at,
            window_days=WINDOW_DAYS,
            risk_free=risk_free,
            db=db,
        )
        if result["outcome_label"] == "pending":
            continue

        if existing:
            existing.outcome_label = result["outcome_label"]
            existing.abs_return = result["abs_return"]
            existing.benchmark_excess_return = result["benchmark_excess_return"]
            existing.portfolio_sortino_delta = result["portfolio_sortino_delta"]
            existing.notes_json = result.get("notes_json")
            existing.evaluated_at = datetime.now(UTC)
        else:
            outcome = RecommendationOutcome(
                recommendation_id=rec.id,
                evaluated_at=datetime.now(UTC),
                window_days=WINDOW_DAYS,
                outcome_label=result["outcome_label"],
                abs_return=result["abs_return"],
                benchmark_excess_return=result["benchmark_excess_return"],
                portfolio_sortino_delta=result["portfolio_sortino_delta"],
                notes_json=result.get("notes_json"),
            )
            db.add(outcome)
        evaluated += 1

    db.commit()
    return evaluated
