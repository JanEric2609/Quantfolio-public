"""Graduation redesign tests (PR2 group D).

Acceptance: a champion sustaining the 4-axis bar over the window passes the
new criterion; a dip on one axis fails it; graduate/de-graduate flips are
persisted with timestamps + reasons; de-graduation pauses rec emission.
"""
from datetime import date, timedelta
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorGraduationTransition,
    AdvisorScorecard,
    AdvisorStrategy,
    PaperPortfolio,
    User,
)
from app.decision.graduation.evaluator import (
    GraduationCriterion,
    GraduationResult,
    evaluate_graduation,
)
from app.decision.graduation.state import apply_graduation_state, get_state, is_graduated


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _champion(db, user: User) -> AdvisorStrategy:
    portfolio = PaperPortfolio(
        user_id=user.id, name="Advisor Loop", initial_cash=100_000,
        managed_by="llm", mandate="advisor",
    )
    db.add(portfolio)
    db.commit()
    strat = AdvisorStrategy(
        user_id=user.id, role="champion", portfolio_id=portfolio.id, config_json={},
    )
    db.add(strat)
    db.commit()
    return strat


def _scorecard(db, user, portfolio_id, *, days_ago: int, brier: float = 0.2,
               sharpe: float = 0.5, mz_slope: float = 0.9, mdd: float = 0.05) -> AdvisorScorecard:
    end = date.today() - timedelta(days=days_ago)
    card = AdvisorScorecard(
        user_id=user.id, portfolio_id=portfolio_id,
        window_start=end - timedelta(days=90), window_end=end,
        n_predictions=30, n_resolved=25,
        sharpe=sharpe, sortino=sharpe, calmar=sharpe,
        brier_avg=brier, log_loss_avg=0.5,
        mz_slope=mz_slope, mz_r2=0.4,
        max_drawdown=mdd, cvar_95=0.02,
    )
    db.add(card)
    db.commit()
    return card


def _result(graduated: bool) -> GraduationResult:
    return GraduationResult(
        graduated=graduated,
        overall_progress=1.0 if graduated else 0.4,
        criteria=[
            GraduationCriterion(
                key="four_axis_bar", label="bar", passed=graduated,
                progress=1.0 if graduated else 0.2, value=None, target=None,
                detail="synthetic",
            )
        ],
        metrics={},
    )


def test_four_axis_bar_sustained_passes_and_dip_fails():
    db = _memory_db()
    user = _user(db)
    champ = _champion(db, user)

    # Three consecutive passing scorecards → the bar criterion passes.
    for days_ago in (14, 7, 0):
        _scorecard(db, user, champ.portfolio_id, days_ago=days_ago)
    result = evaluate_graduation(db, user.id)
    bar = next(c for c in result.criteria if c.key == "four_axis_bar")
    assert bar.passed, bar.detail
    assert result.metrics["champion_strategy_id"] == champ.id
    assert result.metrics["champion_portfolio_id"] == champ.portfolio_id

    # A calibration dip on the newest card breaks the sustained bar.
    _scorecard(db, user, champ.portfolio_id, days_ago=-1, brier=0.4)
    result = evaluate_graduation(db, user.id)
    bar = next(c for c in result.criteria if c.key == "four_axis_bar")
    assert not bar.passed
    assert "brier" in bar.detail


def test_missing_scorecards_fail_the_bar_honestly():
    db = _memory_db()
    user = _user(db)
    _champion(db, user)
    result = evaluate_graduation(db, user.id)
    bar = next(c for c in result.criteria if c.key == "four_axis_bar")
    assert not bar.passed
    assert "scorecards exist" in bar.detail  # insufficient data, not a placeholder


def test_graduation_state_transitions_recorded_with_reasons():
    db = _memory_db()
    user = _user(db)
    _champion(db, user)

    # Flip on.
    state = apply_graduation_state(db, user.id, _result(True))
    assert state.graduated and state.since is not None
    assert is_graduated(db, user.id)

    # Idempotent for an unchanged verdict — no extra transition rows.
    apply_graduation_state(db, user.id, _result(True))
    assert db.query(AdvisorGraduationTransition).count() == 1

    # Flip off (de-graduation) — recorded with the failing criteria.
    state = apply_graduation_state(db, user.id, _result(False))
    assert not state.graduated and state.since is None
    assert not is_graduated(db, user.id)
    transitions = (
        db.query(AdvisorGraduationTransition)
        .order_by(AdvisorGraduationTransition.created_at.asc())
        .all()
    )
    assert len(transitions) == 2
    assert transitions[0].to_graduated is True
    assert transitions[1].to_graduated is False
    assert "four_axis_bar" in transitions[1].reason
    assert transitions[1].criteria_json["four_axis_bar"]["passed"] is False
