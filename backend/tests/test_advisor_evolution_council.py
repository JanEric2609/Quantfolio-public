"""Council wiring into run_evolution_round — closes the CompetitionDecision
.council_result_json gap (Phase B of the council-wiring plan).

Regression guard for the exact bug this wiring exists to fix: every
competition journal entry silently showed "hold" because council_result_json
was never populated. Covers:

1. A real (mocked) competition result is correctly split — the champion's
   row gets portfolio_a's CouncilResult, the challenger's row gets
   portfolio_b's — never swapped, never the wrapping CompetitionCouncilResult.
2. run_evolution_round wires this through end to end without altering
   promotion/scoring (which stays driven by AdvisorScorecard, not the council).
3. A council failure degrades to the pre-existing default, never blocks the
   round.
4. /api/llm-portfolio's get_journal, reading a row produced by this new path,
   surfaces the real trade proposal instead of "hold".
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import CompetitionDecision, CompetitionRun, DiscoveryPrediction, PaperSnapshot, User
from app.decision.advisor.evolution import _run_competition_council, run_evolution_round
from app.decision.advisor.strategy import ensure_champion, spawn_challenger
from app.decision.llm_portfolio.agents.models import (
    AnalysisReport,
    CompetitionCouncilResult,
    CouncilResult,
    DebateReport,
    DecisionReport,
    MacroReport,
    RiskReport,
    TradeProposalItem,
)


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _seed_sleeve(db, user: User, portfolio_id: str, *, good: bool, n_preds: int = 6) -> None:
    today = date.today()
    for i in range(30):
        nav = 100_000 * (1 + (0.004 if good else -0.01)) ** i
        db.add(PaperSnapshot(
            id=uuid4().hex, portfolio_id=portfolio_id,
            date=today - timedelta(days=30 - i), total_value=Decimal(str(round(nav, 2))),
        ))
    now = datetime.now(UTC)
    for i in range(n_preds):
        predicted = 0.02 + i * 0.01
        realised = predicted if good else -predicted
        db.add(DiscoveryPrediction(
            id=uuid4().hex, user_id=user.id, run_id="evo",
            portfolio_id=portfolio_id, symbol=f"S{i}",
            predicted_at=now - timedelta(days=25), horizon_days=21,
            resolve_at=now - timedelta(days=2), direction="buy",
            conviction=0.8 if good else 0.9,
            expected_return=predicted, realised_return=realised,
            outcome_status="resolved",
        ))
    db.commit()


def _fake_competition_result(portfolio_a_id: str, portfolio_b_id: str) -> CompetitionCouncilResult:
    """A CompetitionCouncilResult with distinguishable portfolio_a/portfolio_b markers."""

    def _council(pid: str, asset_id: str) -> CouncilResult:
        return CouncilResult(
            portfolio_id=pid,
            analysis=AnalysisReport(portfolio_id=pid),
            risk=RiskReport(portfolio_id=pid, var_95=0.01, cvar_95=0.02, max_drawdown=0.05),
            macro=MacroReport(portfolio_id=pid),
            decision=DecisionReport(
                portfolio_id=pid,
                trade_proposals=[
                    TradeProposalItem(asset_id=asset_id, action="buy", quantity=1.0, rationale="test")
                ],
                strategy_narrative=f"narrative for {asset_id}",
            ),
        )

    return CompetitionCouncilResult(
        run_id="run-1",
        portfolio_a=_council(portfolio_a_id, "CHAMP_ASSET"),
        portfolio_b=_council(portfolio_b_id, "CHALL_ASSET"),
        debate=DebateReport(portfolio_a_id=portfolio_a_id, portfolio_b_id=portfolio_b_id, narrative="debate"),
    )


def test_run_competition_council_pairs_portfolio_a_with_champion():
    """The champion's context is built first and passed as context_a — its row
    must get portfolio_a's CouncilResult, not portfolio_b's."""
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)

    fake_result = _fake_competition_result(champion.portfolio_id, challenger.portfolio_id)

    with patch(
        "app.decision.llm_portfolio.agents.orchestrator.CompetitionOrchestrator.run_competition",
        new=AsyncMock(return_value=fake_result),
    ):
        champ_json, chall_json, debate_json = _run_competition_council(
            db, champion=champion, challenger=challenger, run_id="run-1",
        )

    assert champ_json is not None and chall_json is not None and debate_json is not None
    champ_decision = json.loads(champ_json)["decision"]
    chall_decision = json.loads(chall_json)["decision"]
    assert champ_decision["trade_proposals"][0]["asset_id"] == "CHAMP_ASSET"
    assert chall_decision["trade_proposals"][0]["asset_id"] == "CHALL_ASSET"
    assert json.loads(debate_json)["narrative"] == "debate"


def test_run_competition_council_degrades_to_none_on_failure():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)

    with patch(
        "app.decision.llm_portfolio.agents.orchestrator.CompetitionOrchestrator.run_competition",
        new=AsyncMock(side_effect=RuntimeError("LLM endpoint unreachable")),
    ):
        result = _run_competition_council(db, champion=champion, challenger=challenger, run_id="run-1")

    assert result == (None, None, None)


def test_run_evolution_round_populates_council_result_json_without_changing_promotion():
    """Wiring the council into the round must not change promotion (scored
    from AdvisorScorecard, not the council) — only council_result_json changes."""
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)
    _seed_sleeve(db, user, champion.portfolio_id, good=False)
    _seed_sleeve(db, user, challenger.portfolio_id, good=True)

    fake_result = _fake_competition_result(champion.portfolio_id, challenger.portfolio_id)

    with patch(
        "app.decision.llm_portfolio.agents.orchestrator.CompetitionOrchestrator.run_competition",
        new=AsyncMock(return_value=fake_result),
    ):
        summary = run_evolution_round(db, user.id, min_resolved=5, reflect=False)

    # Promotion behaviour is exactly as the pre-existing (council-less) tests expect.
    assert summary["verdict"] == "promote"
    assert summary["promoted_strategy_id"] == challenger.id

    run = db.query(CompetitionRun).filter(CompetitionRun.name == "Champion vs Challenger").one()
    champ_row = db.query(CompetitionDecision).filter(
        CompetitionDecision.run_id == run.id,
        CompetitionDecision.portfolio_id == champion.portfolio_id,
    ).one()
    chall_row = db.query(CompetitionDecision).filter(
        CompetitionDecision.run_id == run.id,
        CompetitionDecision.portfolio_id == challenger.portfolio_id,
    ).one()

    assert json.loads(champ_row.council_result_json)["decision"]["trade_proposals"][0]["asset_id"] == "CHAMP_ASSET"
    assert json.loads(chall_row.council_result_json)["decision"]["trade_proposals"][0]["asset_id"] == "CHALL_ASSET"
    assert champ_row.debate_report_json is not None
    assert chall_row.debate_report_json is not None


def test_journal_surfaces_real_proposal_instead_of_hold():
    """Regression guard for the actual production bug: get_journal must show
    the council's real trade proposal, not silently fall back to 'hold'."""
    from app.interface.api.llm_portfolio import get_journal
    from app.foundation.models.entities import PaperPortfolio

    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)
    _seed_sleeve(db, user, champion.portfolio_id, good=False)
    _seed_sleeve(db, user, challenger.portfolio_id, good=True)

    fake_result = _fake_competition_result(champion.portfolio_id, challenger.portfolio_id)
    with patch(
        "app.decision.llm_portfolio.agents.orchestrator.CompetitionOrchestrator.run_competition",
        new=AsyncMock(return_value=fake_result),
    ):
        run_evolution_round(db, user.id, min_resolved=5, reflect=False)

    champ_portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.id == champion.portfolio_id).one()
    result = get_journal.__wrapped__(champ_portfolio.id, user=user, db=db)
    competition_entries = [r for r in result if r["source"] == "competition"]
    assert len(competition_entries) == 1
    entry = competition_entries[0]["decision_json"]
    assert entry["action"] == "buy"
    assert entry["ticker"] == "CHAMP_ASSET"
    assert entry["proposals"][0]["asset_id"] == "CHAMP_ASSET"
