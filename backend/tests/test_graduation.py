"""Tests for the graduation gate: when the LLM may advise the real portfolio."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorScorecard,
    AdvisorStrategy,
    LlmPortfolioDecision,
    PaperPortfolio,
    PaperSnapshot,
    PaperTrade,
    PortfolioSnapshot,
    TrialLedgerEntry,
    User,
)
from app.decision.advisor.cycle import ADVISOR_MANDATE
from app.decision.advisor.strategy import CHALLENGER_MANDATE, ROLE_CHAMPION
from app.decision.graduation import GraduationConfig, evaluate_graduation

_NEW_GATE_CRITERIA = {
    "backtest_overfitting",
    "sufficient_track_record_length",
    "out_of_sample_confirmation",
}


def _seed_variant_returns(db, portfolio_id: str, rates: list[float], final_return_pct: float) -> None:
    """Write daily NAV snapshots following an explicit, deterministic daily-return
    sequence — unlike ``_seed_track_record``'s constant compounding rate, this lets
    a test control period-to-period variance (needed so CSCV/PBO has a real signal
    to rank instead of a zero-variance tie)."""
    start = date(2026, 1, 1)
    nav = 100000.0
    n = len(rates)
    for i, r in enumerate(rates):
        nav *= 1.0 + r
        db.add(
            PaperSnapshot(
                portfolio_id=portfolio_id,
                date=start + timedelta(days=i),
                total_value=Decimal(str(round(nav, 2))),
                total_return_pct=Decimal(str(final_return_pct)) if i == n - 1 else Decimal("0"),
            )
        )
    db.commit()


def _seed_trades_and_decisions(db, portfolio_id: str, n_trades: int = 22, n_decisions: int = 6) -> None:
    """Decision-sample and learning-maturity criteria need trades/reviews
    that ``_seed_variant_returns`` (NAV-only) doesn't provide."""
    for i in range(n_trades):
        db.add(
            PaperTrade(
                portfolio_id=portfolio_id, ticker=f"TKR{i}", side="buy",
                quantity=Decimal("1"), price=Decimal("100"), value=Decimal("100"),
            )
        )
    for _ in range(n_decisions):
        db.add(
            LlmPortfolioDecision(
                portfolio_id=portfolio_id, review_date=datetime.now(timezone.utc), status="complete",
            )
        )
    db.commit()


def _register_champion(db, user_id: str, portfolio_id: str, registered_at: datetime) -> None:
    """Mirror advisor/strategy.py's champion-promotion ledger registration
    (ADR 0015 condition 5) with an explicit, test-controlled timestamp."""
    strategy = AdvisorStrategy(
        user_id=user_id, role=ROLE_CHAMPION, portfolio_id=portfolio_id, config_json={},
    )
    db.add(strategy)
    db.flush()
    db.add(
        TrialLedgerEntry(
            context="advisor_champion",
            trial_key=strategy.id,
            registered_at=registered_at,
            metadata_json={},
        )
    )
    db.commit()


def _seed_passing_scorecards(db, user_id: str, portfolio_id: str, n: int = 3) -> None:
    """Seed *n* consecutive scorecards that clear every 4-axis floor (PR2)."""
    for i in range(n):
        end = date.today() - timedelta(days=(n - 1 - i) * 7)
        db.add(
            AdvisorScorecard(
                user_id=user_id, portfolio_id=portfolio_id,
                window_start=end - timedelta(days=90), window_end=end,
                n_predictions=30, n_resolved=25,
                sharpe=1.2, sortino=1.5, calmar=1.0,
                brier_avg=0.15, log_loss_avg=0.45,
                mz_slope=0.95, mz_r2=0.4,
                max_drawdown=0.04, cvar_95=0.015,
            )
        )
    db.commit()


def _user(db) -> User:
    """
    Create and persist a test user to the database.
    
    Returns:
    	User: The persisted User object.
    """
    user = User(username="grace", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _ai_portfolio(db, user_id: str) -> PaperPortfolio:
    """
    Create and persist the advisor loop's champion paper portfolio for a user.

    Uses ADVISOR_MANDATE (not an llm_portfolio mandate A/B string) — graduation
    evaluates the advisor loop specifically, and _ai_portfolios() scopes to
    ADVISOR_MANDATE/CHALLENGER_MANDATE.

    Parameters:
        user_id (str): The user ID to associate with the portfolio.

    Returns:
        portfolio (PaperPortfolio): The persisted advisor champion portfolio instance.
    """
    p = PaperPortfolio(
        user_id=user_id,
        name="Advisor Champion",
        mandate=ADVISOR_MANDATE,
        managed_by="llm",
        initial_cash=Decimal("100000"),
        baseline_value=Decimal("100000"),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _seed_track_record(
    db,
    portfolio_id: str,
    *,
    days: int,
    daily_return: float,
    final_return_pct: float,
) -> None:
    """
    Populate the test database with synthetic paper portfolio performance history.

    ``final_return_pct`` is a FRACTION (0.18 = 18%), matching the storage
    convention in paper_portfolio.py: total_return_pct = (total_value - baseline) / baseline.

    Creates daily portfolio snapshots spanning the specified number of days with compounding NAV values, adds 22 trades, and records 6 LLM review decisions.
    """
    start = date(2026, 1, 1)
    nav = 100000.0
    for i in range(days):
        nav *= 1.0 + daily_return
        db.add(
            PaperSnapshot(
                portfolio_id=portfolio_id,
                date=start + timedelta(days=i),
                total_value=Decimal(str(round(nav, 2))),
                total_return_pct=Decimal(str(final_return_pct)) if i == days - 1 else Decimal("0"),
            )
        )
    for i in range(22):
        db.add(
            PaperTrade(
                portfolio_id=portfolio_id,
                ticker=f"TKR{i}",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("100"),
                value=Decimal("100"),
            )
        )
    for i in range(6):
        db.add(
            LlmPortfolioDecision(
                portfolio_id=portfolio_id,
                review_date=datetime.now(timezone.utc),
                status="complete",
            )
        )
    db.commit()


def test_no_history_is_not_graduated_but_returns_full_criteria():
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)

    result = evaluate_graduation(db, user.id)
    assert result.graduated is False
    keys = {c.key for c in result.criteria}
    assert keys == {
        "track_length",
        "statistical_skill",
        "consistency",
        "drawdown",
        "outperformance",
        "decision_sample",
        "learning_maturity",
        "four_axis_bar",
    } | _NEW_GATE_CRITERIA
    # The payload carries the safety disclaimers.
    payload = result.to_dict()
    assert payload["not_financial_advice"] is True
    assert payload["estimate"] is True


def test_unassessable_pbo_fails_closed_but_is_not_reported_as_measured():
    """With fewer than two paper variants, PBO cannot be computed. The gate
    must still fail closed, but the dashboard must not show the fail-closed
    sentinel as a measured "PBO 100.0%"."""
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)

    result = evaluate_graduation(db, user.id)
    pbo_criterion = next(c for c in result.criteria if c.key == "backtest_overfitting")
    assert pbo_criterion.passed is False
    assert pbo_criterion.progress == 0.0
    assert pbo_criterion.value is None
    assert result.metrics["pbo"] is None


def test_strong_consistent_record_graduates():
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    # Steady, mildly oscillating ~0.5%/day (always positive: high Sharpe, tiny
    # drawdown, both halves up) — real (non-degenerate) variance so CSCV/PBO
    # has a genuine signal to rank rather than a zero-variance tie.
    _seed_variant_returns(db, p.id, [0.006, 0.004] * 40, final_return_pct=0.18)
    _seed_trades_and_decisions(db, p.id)
    # A second, clearly weaker search variant (occasional negative days) so
    # the PBO criterion has ≥2 candidates and the champion demonstrably,
    # consistently generalises out-of-sample against it.
    pb = PaperPortfolio(
        user_id=user.id, name="Advisor Challenger", mandate=CHALLENGER_MANDATE,
        managed_by="llm", initial_cash=Decimal("100000"), baseline_value=Decimal("100000"),
    )
    db.add(pb)
    db.commit()
    db.refresh(pb)
    _seed_variant_returns(db, pb.id, [0.006, -0.004] * 40, final_return_pct=0.05)
    # Champion registered in the trial ledger well before most of its NAV
    # history — out-of-sample confirmation (ADR 0015 condition 5).
    _register_champion(db, user.id, p.id, datetime(2026, 1, 5, tzinfo=timezone.utc))
    # Real portfolio underperforms the paper book.
    # total_return_pct is stored as a fraction: 0.05 = 5%
    db.add(
        PortfolioSnapshot(
            user_id=user.id,
            date=date(2026, 3, 21),
            total_value=Decimal("105000"),
            total_return_pct=Decimal("0.05"),
        )
    )
    db.commit()
    # PR2: graduation additionally demands a sustained 4-axis bar.
    _seed_passing_scorecards(db, user.id, p.id)

    result = evaluate_graduation(db, user.id)
    failed = [c.key for c in result.criteria if not c.passed]
    assert result.graduated is True, f"unexpected failing criteria: {failed}"
    assert result.metrics["dsr"] >= 0.95
    assert result.overall_progress >= 0.99


def test_n_trials_reflects_search_breadth():
    # The DSR must deflate for distinct strategy variants evaluated during
    # search (competition rounds), not routine mandate reviews of the same
    # strategy — a review re-optimises the champion's existing sleeve, it
    # doesn't introduce a new variant to select among.
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    # _seed_track_record adds 6 completed LlmPortfolioDecision review cycles.
    _seed_track_record(db, p.id, days=80, daily_return=0.003, final_return_pct=0.18)

    result = evaluate_graduation(db, user.id)
    # Reviews are still tallied for observability, but they no longer feed
    # n_trials — n_trials is the global, ledger-backed count (ADR 0015,
    # Finding F15). The 500 floor was dropped on 2026-10-04 for the real
    # count, so an empty ledger is one trial: this one.
    assert result.metrics["n_review_cycles"] == 6
    assert result.metrics["n_competition_decisions"] == 0
    assert result.metrics["n_trials"] == 1


def test_drawdown_breach_blocks_graduation():
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    # Build a path that gains then crashes >25% to violate the drawdown ceiling.
    start = date(2026, 1, 1)
    navs = [100000.0 * (1.03**i) for i in range(40)]  # run up
    navs += [navs[-1] * (0.95**i) for i in range(1, 25)]  # ~ -70% drawdown
    for i, nav in enumerate(navs):
        db.add(
            PaperSnapshot(
                portfolio_id=p.id,
                date=start + timedelta(days=i),
                total_value=Decimal(str(round(nav, 2))),
                total_return_pct=Decimal("0"),
            )
        )
    db.commit()

    result = evaluate_graduation(db, user.id)
    dd_crit = next(c for c in result.criteria if c.key == "drawdown")
    assert dd_crit.passed is False
    assert result.graduated is False


def test_track_length_passes_at_exact_min_observations():
    # Boundary: exactly min_observations daily NAV snapshots must pass, guarding
    # against an off-by-one between NAV observations and the N-1 returns.
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    _seed_track_record(db, p.id, days=60, daily_return=0.001, final_return_pct=0.06)

    result = evaluate_graduation(db, user.id, config=GraduationConfig(min_observations=60))
    track = next(c for c in result.criteria if c.key == "track_length")
    assert track.value == 60.0
    assert track.passed is True


def test_config_thresholds_are_respected():
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    _seed_track_record(db, p.id, days=40, daily_return=0.003, final_return_pct=0.12)

    strict = GraduationConfig(min_observations=500)
    result = evaluate_graduation(db, user.id, config=strict)
    track = next(c for c in result.criteria if c.key == "track_length")
    assert track.passed is False
    assert track.target == 500.0


# --- API gating ------------------------------------------------------------


def _api_client(db, user):
    """
    Create a FastAPI test client with overridden dependencies.
    
    Injects the provided database session and user into the app's dependency
    injection for all subsequent test requests.
    
    Parameters:
        db: Database session
        user: User
    
    Returns:
        TestClient: Configured test client
    """
    from fastapi.testclient import TestClient

    from app.foundation.core.db import get_db
    from app.main import app
    from app.foundation.auth import current_user

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def test_status_endpoint_reports_progress():
    """
    Validate that the graduation status endpoint returns the expected response structure with graduation indicators and safety disclaimers.
    """
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)
    client = _api_client(db, user)
    try:
        resp = client.get("/api/graduation/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["graduated"] is False
        assert "criteria" in body and len(body["criteria"]) == 11
        assert body["recommendations_unlocked"] is False
        assert body["not_financial_advice"] is True
    finally:
        app_overrides_clear()


def test_graduated_user_can_post_recommendations():
    """Gate retirement: recommendations are gated by graduation alone — a
    graduated user can POST /api/graduation/recommendations with no
    experimental flag involved."""
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    # Same fully-passing setup as the evaluator tests (oscillating ~0.5%/day,
    # real book underperforms, a weaker second variant for PBO, and a ledger
    # registration for the out-of-sample-confirmation criterion).
    _seed_variant_returns(db, p.id, [0.006, 0.004] * 40, final_return_pct=0.18)
    _seed_trades_and_decisions(db, p.id)
    pb = PaperPortfolio(
        user_id=user.id, name="Advisor Challenger", mandate=CHALLENGER_MANDATE,
        managed_by="llm", initial_cash=Decimal("100000"), baseline_value=Decimal("100000"),
    )
    db.add(pb)
    db.commit()
    db.refresh(pb)
    _seed_variant_returns(db, pb.id, [0.006, -0.004] * 40, final_return_pct=0.05)
    _register_champion(db, user.id, p.id, datetime(2026, 1, 5, tzinfo=timezone.utc))
    db.add(
        PortfolioSnapshot(
            user_id=user.id,
            date=date(2026, 3, 21),
            total_value=Decimal("105000"),
            total_return_pct=Decimal("0.05"),
        )
    )
    db.commit()
    _seed_passing_scorecards(db, user.id, p.id)

    result = evaluate_graduation(db, user.id)
    failed = [c.key for c in result.criteria if not c.passed]
    assert result.graduated is True, f"unexpected failing criteria: {failed}"

    client = _api_client(db, user)
    try:
        fake_item = SimpleNamespace(
            ticker="EUNL.DE", action="buy", confidence=0.7, thesis="diversified core", risks=[]
        )
        fake_report = SimpleNamespace(report_id="rep-1", recommendations=[fake_item])
        with patch(
            "app.decision.recommendation_engine.orchestrator.generate_recommendations",
            new_callable=AsyncMock,
            return_value={"report": fake_report},
        ):
            resp = client.post("/api/graduation/recommendations")
        assert resp.status_code == 200
        body = resp.json()
        assert body["graduation"]["graduated"] is True
        assert body["recommendations"][0]["ticker"] == "EUNL.DE"
        assert body["estimate"] is True
        assert body["not_tax_advice"] is True
        assert body["not_financial_advice"] is True
    finally:
        app_overrides_clear()


def app_overrides_clear():
    """
    Clear FastAPI dependency overrides to reset test dependency injection.
    """
    from app.main import app

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Additional tests: helpers, edge cases, and boundary conditions


def test_no_ai_portfolios_returns_all_failing_criteria():
    """A user with no LLM-managed portfolios must never graduate."""
    db = _memory_db()
    user = _user(db)
    # No portfolios created at all.
    result = evaluate_graduation(db, user.id)
    assert result.graduated is False
    assert result.overall_progress == 0.0
    for c in result.criteria:
        assert c.passed is False
        assert c.progress == 0.0


def test_graduation_config_defaults():
    cfg = GraduationConfig()
    assert cfg.min_observations == 60
    assert cfg.min_dsr == 0.95
    assert cfg.max_drawdown == 0.25
    assert cfg.min_outperformance == 0.0
    assert cfg.min_decisions == 20
    assert cfg.min_learning_cycles == 5


def test_graduation_result_to_dict_contains_required_keys():
    db = _memory_db()
    user = _user(db)
    result = evaluate_graduation(db, user.id)
    d = result.to_dict()
    for key in ("graduated", "overall_progress", "criteria", "metrics",
                "generated_at", "estimate", "not_tax_advice", "not_financial_advice"):
        assert key in d, f"Missing key in to_dict(): {key}"
    assert d["estimate"] is True
    assert d["not_tax_advice"] is True
    assert d["not_financial_advice"] is True


def test_outperformance_criterion_passes_when_paper_beats_real():
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    _seed_track_record(db, p.id, days=10, daily_return=0.005, final_return_pct=0.10)
    # Real portfolio lags paper.
    # total_return_pct stored as fraction: 0.03 = 3%
    db.add(
        PortfolioSnapshot(
            user_id=user.id,
            date=date(2026, 1, 10),
            total_value=Decimal("103000"),
            total_return_pct=Decimal("0.03"),
        )
    )
    db.commit()

    result = evaluate_graduation(db, user.id)
    outperf = next(c for c in result.criteria if c.key == "outperformance")
    assert outperf.passed is True
    assert result.metrics["paper_total_return"] == 0.10
    assert result.metrics["real_total_return"] == pytest.approx(0.03, abs=1e-6)
    assert result.metrics["outperformance_edge"] == pytest.approx(0.07, abs=1e-4)


def test_outperformance_criterion_fails_when_paper_trails_real():
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    _seed_track_record(db, p.id, days=10, daily_return=0.001, final_return_pct=0.02)
    # Real portfolio beats paper.
    # total_return_pct stored as fraction: 0.15 = 15%
    db.add(
        PortfolioSnapshot(
            user_id=user.id,
            date=date(2026, 1, 10),
            total_value=Decimal("115000"),
            total_return_pct=Decimal("0.15"),
        )
    )
    db.commit()

    result = evaluate_graduation(db, user.id)
    outperf = next(c for c in result.criteria if c.key == "outperformance")
    assert outperf.passed is False
    assert result.metrics["outperformance_edge"] is not None
    assert result.metrics["outperformance_edge"] < 0


def test_outperformance_criterion_fails_without_real_portfolio():
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    _seed_track_record(db, p.id, days=10, daily_return=0.003, final_return_pct=0.08)
    # No PortfolioSnapshot added.

    result = evaluate_graduation(db, user.id)
    outperf = next(c for c in result.criteria if c.key == "outperformance")
    assert outperf.passed is False
    assert outperf.progress == 0.0
    assert result.metrics["real_total_return"] is None


def test_decision_sample_exactly_at_boundary():
    """19 trades fails; 20 trades passes."""
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)

    # Seed 10 days of returns only (no trades/learning yet)
    start = date(2026, 1, 1)
    nav = 100000.0
    for i in range(10):
        nav *= 1.003
        db.add(
            PaperSnapshot(
                portfolio_id=p.id,
                date=start + timedelta(days=i),
                total_value=Decimal(str(round(nav, 2))),
                total_return_pct=Decimal("0"),
            )
        )
    # Exactly 19 trades — one below the threshold.
    for i in range(19):
        db.add(
            PaperTrade(
                portfolio_id=p.id,
                ticker=f"X{i}",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("50"),
                value=Decimal("50"),
            )
        )
    db.commit()

    result = evaluate_graduation(db, user.id)
    dec = next(c for c in result.criteria if c.key == "decision_sample")
    assert dec.passed is False
    assert dec.value == 19.0

    # Add the 20th trade — now it should pass.
    db.add(
        PaperTrade(
            portfolio_id=p.id,
            ticker="X19",
            side="sell",
            quantity=Decimal("1"),
            price=Decimal("55"),
            value=Decimal("55"),
        )
    )
    db.commit()

    result2 = evaluate_graduation(db, user.id)
    dec2 = next(c for c in result2.criteria if c.key == "decision_sample")
    assert dec2.passed is True
    assert dec2.value == 20.0


def test_learning_maturity_boundary():
    """4 completed reviews fails; 5 passes."""
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)

    # Minimal snapshots so portfolio is recognised.
    start = date(2026, 1, 1)
    nav = 100000.0
    for i in range(5):
        nav *= 1.003
        db.add(
            PaperSnapshot(
                portfolio_id=p.id,
                date=start + timedelta(days=i),
                total_value=Decimal(str(round(nav, 2))),
                total_return_pct=Decimal("0"),
            )
        )
    for _ in range(4):
        db.add(
            LlmPortfolioDecision(
                portfolio_id=p.id,
                review_date=datetime.now(timezone.utc),
                status="complete",
            )
        )
    db.commit()

    result = evaluate_graduation(db, user.id)
    lm = next(c for c in result.criteria if c.key == "learning_maturity")
    assert lm.passed is False
    assert lm.value == 4.0

    # Add the 5th cycle.
    db.add(
        LlmPortfolioDecision(
            portfolio_id=p.id,
            review_date=datetime.now(timezone.utc),
            status="complete",
        )
    )
    db.commit()

    result2 = evaluate_graduation(db, user.id)
    lm2 = next(c for c in result2.criteria if c.key == "learning_maturity")
    assert lm2.passed is True
    assert lm2.value == 5.0


def test_consistency_criterion_fails_with_one_negative_half():
    """A track record whose second half is negative fails the consistency check."""
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)

    start = date(2026, 1, 1)
    # First half: rises; second half: falls.
    navs = [100000.0 * (1.01 ** i) for i in range(15)]        # steady up
    navs += [navs[-1] * (0.99 ** i) for i in range(1, 16)]    # steady down
    for i, nav in enumerate(navs):
        db.add(
            PaperSnapshot(
                portfolio_id=p.id,
                date=start + timedelta(days=i),
                total_value=Decimal(str(round(nav, 2))),
                total_return_pct=Decimal("0"),
            )
        )
    db.commit()

    result = evaluate_graduation(db, user.id)
    cons = next(c for c in result.criteria if c.key == "consistency")
    assert cons.passed is False
    assert cons.progress == 0.5  # one of two halves positive


def test_metrics_contains_expected_keys():
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)

    result = evaluate_graduation(db, user.id)
    expected_keys = {
        "n_ai_portfolios", "n_trials", "champion_portfolio_id",
        "dsr", "psr", "sharpe", "sortino", "min_track_record_length", "observations",
        "max_drawdown", "paper_total_return", "real_total_return", "outperformance_edge",
        "n_decisions", "learning_cycles",
        "competition_wins", "competition_rounds", "competition_win_rate",
    }
    for key in expected_keys:
        assert key in result.metrics, f"Missing metrics key: {key}"


def test_multiple_ai_portfolios_champion_is_higher_dsr():
    """With two AI portfolios the evaluator selects the better-performing one."""
    db = _memory_db()
    user = _user(db)

    # Portfolio A (advisor champion): strong signal, many observations.
    pa = _ai_portfolio(db, user.id)
    _seed_track_record(db, pa.id, days=80, daily_return=0.003, final_return_pct=0.18)

    # Portfolio B (advisor challenger): weak, fewer observations — should lose
    # champion selection. Uses CHALLENGER_MANDATE, not an llm_portfolio
    # mandate A/B string — this test exercises the fallback champion-selection
    # loop within advisor's own two candidate portfolios. (The loop used to
    # scope to every managed_by=="llm" portfolio, which would have wrongly
    # considered llm_portfolio's mandate A/B sleeves as candidates too.)
    pb = PaperPortfolio(
        user_id=user.id,
        name="Advisor Challenger",
        mandate=CHALLENGER_MANDATE,
        managed_by="llm",
        initial_cash=Decimal("100000"),
        baseline_value=Decimal("100000"),
    )
    db.add(pb)
    db.commit()
    db.refresh(pb)
    _seed_track_record(db, pb.id, days=10, daily_return=0.0001, final_return_pct=0.005)

    result = evaluate_graduation(db, user.id)
    # Champion should be portfolio A (higher DSR).
    assert result.metrics["champion_portfolio_id"] == pa.id
    assert result.metrics["n_ai_portfolios"] == 2
    # n_trials is deflated by search breadth (#107), so it floors at the
    # portfolio count but grows with review/competition cycles.
    assert result.metrics["n_trials"] >= result.metrics["n_ai_portfolios"]


def test_status_endpoint_includes_passive_core_block():
    """ADR 0015 Phase 2: the silent-period default is always present on
    /status regardless of graduation verdict, so the frontend can surface it."""
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)
    client = _api_client(db, user)
    try:
        resp = client.get("/api/graduation/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["graduated"] is False
        assert body["passive_core"]["ticker"] == "EUNL.DE"
        assert body["passive_core"]["isin"] == "IE00B4L5Y983"
        assert body["passive_core"]["mode"] == "passive_core"
    finally:
        app_overrides_clear()


def test_recommendations_locked_response_includes_passive_core_block():
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)
    client = _api_client(db, user)
    try:
        resp = client.post("/api/graduation/recommendations")
        assert resp.status_code == 409
        body = resp.json()["error"]["message"]
        assert body["code"] == "not_graduated"
        assert body["passive_core"]["ticker"] == "EUNL.DE"
    finally:
        app_overrides_clear()


def test_history_endpoint_returns_list():
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)
    client = _api_client(db, user)
    try:
        # First call to /status persists a snapshot; history should then have it.
        client.get("/api/graduation/status")
        resp = client.get("/api/graduation/history")
        assert resp.status_code == 200
        rows = resp.json()
        assert isinstance(rows, list)
        assert len(rows) >= 1
        row = rows[0]
        assert "graduated" in row
        assert "overall_progress" in row
        assert "created_at" in row
    finally:
        app_overrides_clear()


def test_history_endpoint_limit_is_respected():
    db = _memory_db()
    user = _user(db)
    _ai_portfolio(db, user.id)
    client = _api_client(db, user)
    try:
        # Persist three snapshots.
        for _ in range(3):
            client.get("/api/graduation/status")
        resp = client.get("/api/graduation/history?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) <= 2
    finally:
        app_overrides_clear()


def test_nav_returns_helper_computes_correct_returns():
    """_nav_returns converts NAV snapshots to per-period simple returns."""
    from app.decision.graduation.evaluator import _nav_returns

    # Manually build mock-like objects with total_value attribute.
    class _Snap:
        def __init__(self, v):
            self.total_value = Decimal(str(v))

    snaps = [_Snap(100), _Snap(110), _Snap(99), _Snap(110)]
    rets = _nav_returns(snaps)
    assert len(rets) == 3
    assert abs(rets[0] - 0.10) < 1e-9    # 100 -> 110: +10%
    assert abs(rets[1] - (-0.10)) < 1e-6  # 110 -> 99: -10%


def test_nav_returns_skips_none_total_value():
    from app.decision.graduation.evaluator import _nav_returns

    class _Snap:
        def __init__(self, v):
            self.total_value = v

    snaps = [_Snap(100), _Snap(None), _Snap(110)]
    rets = _nav_returns(snaps)
    # None is filtered; only 100 and 110 remain → one return.
    assert len(rets) == 1
    assert abs(rets[0] - 0.10) < 1e-9


def test_clamp01_clamps_correctly():
    from app.decision.graduation.evaluator import _clamp01

    assert _clamp01(-1.0) == 0.0
    assert _clamp01(0.0) == 0.0
    assert _clamp01(0.5) == 0.5
    assert _clamp01(1.0) == 1.0
    assert _clamp01(2.0) == 1.0


# ---------------------------------------------------------------------------
# BUG-2 regression: outperformance uses fractional returns (no /100 bug)
# ---------------------------------------------------------------------------

def test_outperformance_fractional_storage_convention():
    """total_return_pct is stored as fraction (0.05 = 5%), not percent (5.0).

    BUG-2: evaluator.py previously divided by 100 again, making 0.05 → 0.0005,
    so a paper portfolio returning 18% would only compare as 0.18% vs the real
    portfolio.  The outperformance criterion would almost always appear to fail
    even when the AI clearly beat the real portfolio.

    This test verifies the fix: returns are used as-is from the DB.
    """
    db = _memory_db()
    user = _user(db)
    p = _ai_portfolio(db, user.id)
    # Paper returned 15% → stored as 0.15 (fraction)
    _seed_track_record(db, p.id, days=10, daily_return=0.005, final_return_pct=0.15)
    # Real portfolio returned 5% → stored as 0.05 (fraction)
    db.add(
        PortfolioSnapshot(
            user_id=user.id,
            date=date(2026, 1, 10),
            total_value=Decimal("105000"),
            total_return_pct=Decimal("0.05"),  # fraction, not percent
        )
    )
    db.commit()

    result = evaluate_graduation(db, user.id)
    outperf = next(c for c in result.criteria if c.key == "outperformance")

    # With the /100 bug: paper=0.0015, real=0.0005, edge=0.001 (≥0 passes but
    # values would be obviously wrong in the UI).  Without the bug:
    assert result.metrics["paper_total_return"] == pytest.approx(0.15, abs=1e-9), (
        "paper_total_return should be 0.15 (15%), was the /100 bug applied?"
    )
    assert result.metrics["real_total_return"] == pytest.approx(0.05, abs=1e-9), (
        "real_total_return should be 0.05 (5%), was the /100 bug applied?"
    )
    assert result.metrics["outperformance_edge"] == pytest.approx(0.10, abs=1e-6)
    assert outperf.passed is True


def test_outperformance_real_return_is_read_as_fraction_not_percent():
    """_real_total_return must return the raw fractional value without /100.

    Direct test of the helper — confirms no double-conversion occurs.
    """
    from app.decision.graduation.evaluator import _real_total_return

    db = _memory_db()
    user = _user(db)
    # Store 7% as a fraction (the convention used by portfolio_service.py)
    db.add(
        PortfolioSnapshot(
            user_id=user.id,
            date=date(2026, 1, 10),
            total_value=Decimal("107000"),
            total_return_pct=Decimal("0.07"),
        )
    )
    db.commit()

    val = _real_total_return(db, user.id)
    assert val == pytest.approx(0.07, abs=1e-9), (
        f"Expected 0.07 (7% as fraction), got {val}.  "
        "If this is 0.0007 the /100 bug is present."
    )

