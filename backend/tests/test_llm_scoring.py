"""Tests for mandate-review outcome scoring + the accuracy endpoint."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import _memory_db

from app.interface.api.llm_portfolio import get_accuracy
from app.foundation.models.entities import LlmPortfolioDecision, PaperPortfolio, PaperSnapshot, User
from app.decision.llm_portfolio import scoring


def _user(db) -> User:
    user = User(username="scorer", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _portfolio(db, user) -> PaperPortfolio:
    p = PaperPortfolio(user_id=user.id, name="Mandate A", mandate="A")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _decision(db, portfolio, *, weeks_ago: int, horizon: int, expectation: dict) -> LlmPortfolioDecision:
    d = LlmPortfolioDecision(
        portfolio_id=portfolio.id,
        review_date=datetime.now(UTC) - timedelta(weeks=weeks_ago),
        mandate="A",
        status="completed",
        decision_json=json.dumps({"action": "buy", "ticker": "AAPL", "expectation": expectation}),
        horizon_weeks=horizon,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


# ── score_expectation (pure) ───────────────────────────────────────────────


def test_score_expectation_increase_hit():
    assert scoring.score_expectation(2.5, {"direction": "increase", "magnitude": 2.0}) == "hit"


def test_score_expectation_increase_partial():
    assert scoring.score_expectation(1.0, {"direction": "increase", "magnitude": 2.0}) == "partial"


def test_score_expectation_increase_miss():
    assert scoring.score_expectation(-0.5, {"direction": "increase", "magnitude": 2.0}) == "miss"


def test_score_expectation_decrease_hit():
    # Drawdown got worse than -5% target → "decrease" direction hit.
    assert scoring.score_expectation(-6.0, {"direction": "decrease", "magnitude": 5.0}) == "hit"


def test_score_expectation_decrease_partial():
    assert scoring.score_expectation(-2.0, {"direction": "decrease", "magnitude": 5.0}) == "partial"


def test_score_expectation_unknown_direction():
    assert scoring.score_expectation(1.0, {"direction": "sideways", "magnitude": 2.0}) == "unresolvable"


# ── score_decision (DB) ────────────────────────────────────────────────────


def test_score_decision_abs_return_hit():
    db = _memory_db()
    user = _user(db)
    p = _portfolio(db, user)
    d = _decision(
        db, p, weeks_ago=4, horizon=2,
        expectation={"metric": "abs_return_pct", "direction": "increase", "magnitude": 1.5},
    )
    review = d.review_date
    # Snapshot at review start (0%) and at horizon end (+2.02% since inception).
    db.add(PaperSnapshot(portfolio_id=p.id, date=review.date(), total_return_pct=0))
    db.add(PaperSnapshot(portfolio_id=p.id, date=(review + timedelta(weeks=2)).date(), total_return_pct=2.02))
    db.commit()

    verdict = scoring.score_decision(db, d, datetime.now(UTC))
    assert verdict == "hit"
    reflection = json.loads(d.reflection_json)
    assert reflection["actual_outcome"]["metric"] == "abs_return_pct"
    assert reflection["actual_outcome"]["actual_pct"] == 2.02


def test_score_decision_skips_when_horizon_not_elapsed():
    db = _memory_db()
    user = _user(db)
    p = _portfolio(db, user)
    d = _decision(
        db, p, weeks_ago=1, horizon=8,
        expectation={"metric": "abs_return_pct", "direction": "increase", "magnitude": 1.0},
    )
    assert scoring.score_decision(db, d, datetime.now(UTC)) is None


def test_score_decision_unresolvable_without_snapshots():
    db = _memory_db()
    user = _user(db)
    p = _portfolio(db, user)
    d = _decision(
        db, p, weeks_ago=4, horizon=2,
        expectation={"metric": "abs_return_pct", "direction": "increase", "magnitude": 1.0},
    )
    verdict = scoring.score_decision(db, d, datetime.now(UTC))
    assert verdict == "unresolvable"
    assert json.loads(d.reflection_json)["actual_outcome"]["actual_pct"] is None


def test_score_decision_benchmark_excess(monkeypatch):
    db = _memory_db()
    user = _user(db)
    p = _portfolio(db, user)
    d = _decision(
        db, p, weeks_ago=4, horizon=2,
        expectation={"metric": "benchmark_excess_pct", "direction": "increase", "magnitude": 1.0},
    )
    review = d.review_date
    db.add(PaperSnapshot(portfolio_id=p.id, date=review.date(), total_return_pct=0))
    db.add(PaperSnapshot(portfolio_id=p.id, date=(review + timedelta(weeks=2)).date(), total_return_pct=2.02))
    db.commit()
    # Portfolio +2.0%, benchmark +0.6% → excess +1.4% ≥ 1.0 → hit.
    monkeypatch.setattr(scoring, "_fetch_interval_return", lambda *a, **k: 0.006)
    assert scoring.score_decision(db, d, datetime.now(UTC)) == "hit"


# ── run_scoring ────────────────────────────────────────────────────────────


def test_run_scoring_scores_eligible_only():
    db = _memory_db()
    user = _user(db)
    p = _portfolio(db, user)
    # Eligible: horizon elapsed, snapshots present.
    d1 = _decision(
        db, p, weeks_ago=4, horizon=2,
        expectation={"metric": "abs_return_pct", "direction": "increase", "magnitude": 1.0},
    )
    db.add(PaperSnapshot(portfolio_id=p.id, date=d1.review_date.date(), total_return_pct=0))
    db.add(PaperSnapshot(portfolio_id=p.id, date=(d1.review_date + timedelta(weeks=2)).date(), total_return_pct=3.0))
    # Not eligible: horizon not elapsed.
    _decision(
        db, p, weeks_ago=1, horizon=8,
        expectation={"metric": "abs_return_pct", "direction": "increase", "magnitude": 1.0},
    )
    db.commit()

    count = scoring.run_scoring(db)
    assert count == 1
    db.refresh(d1)
    assert d1.verdict == "hit"
    assert d1.scored_at is not None


def test_run_scoring_idempotent():
    db = _memory_db()
    user = _user(db)
    p = _portfolio(db, user)
    d1 = _decision(
        db, p, weeks_ago=4, horizon=2,
        expectation={"metric": "abs_return_pct", "direction": "increase", "magnitude": 1.0},
    )
    db.add(PaperSnapshot(portfolio_id=p.id, date=d1.review_date.date(), total_return_pct=0))
    db.add(PaperSnapshot(portfolio_id=p.id, date=(d1.review_date + timedelta(weeks=2)).date(), total_return_pct=3.0))
    db.commit()
    assert scoring.run_scoring(db) == 1
    # Second run finds no unscored eligible decisions.
    assert scoring.run_scoring(db) == 0


# ── accuracy endpoint ──────────────────────────────────────────────────────


def test_accuracy_endpoint_aggregates_verdicts():
    db = _memory_db()
    user = _user(db)
    p = _portfolio(db, user)
    now = datetime.now(UTC)
    for verdict in ["hit", "hit", "miss", "partial", "unresolvable"]:
        db.add(LlmPortfolioDecision(
            portfolio_id=p.id, review_date=now, mandate="A", status="completed",
            decision_json="{}", horizon_weeks=2, verdict=verdict, scored_at=now,
        ))
    # One pending (horizon set, no verdict).
    db.add(LlmPortfolioDecision(
        portfolio_id=p.id, review_date=now, mandate="A", status="completed",
        decision_json="{}", horizon_weeks=2,
    ))
    db.commit()

    result = get_accuracy(p.id, user=user, db=db)
    assert result["counts"]["hit"] == 2
    assert result["resolved"] == 4  # hit+hit+miss+partial (unresolvable excluded)
    assert result["hit_rate"] == 0.5
    assert result["pending_scoring"] == 1
    assert result["estimate"] is True
