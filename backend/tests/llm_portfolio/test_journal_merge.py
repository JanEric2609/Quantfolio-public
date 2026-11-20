"""Tests for the merged journal endpoint (review + competition decisions).

Verifies that GET /api/llm-portfolio/{id}/journal returns both
LlmPortfolioDecision rows and CompetitionDecision rows, merged and sorted
chronologically (DESC), with correct 'source' tags.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    LlmPortfolioDecision,
    PaperPortfolio,
    User,
)
from app.foundation.models.entities.competition import CompetitionDecision, CompetitionRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _user(db) -> User:
    user = User(id=uuid4().hex, username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _portfolio(db, user_id: str, mandate: str = "A") -> PaperPortfolio:
    p = PaperPortfolio(
        id=uuid4().hex,
        user_id=user_id,
        name=f"Portfolio {mandate}",
        mandate=mandate,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _review_decision(db, portfolio_id: str, mandate: str, offset_hours: int = 0) -> LlmPortfolioDecision:
    ts = NOW + timedelta(hours=offset_hours)
    d = LlmPortfolioDecision(
        id=uuid4().hex,
        portfolio_id=portfolio_id,
        mandate=mandate,
        review_date=ts,
        decision_json=json.dumps({"action": "buy", "ticker": "AAPL", "thesis": "growth"}),
        reflection_json=None,
        context_token_estimate=500,
        status="completed",
        created_at=ts,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _competition_run(db, portfolio_a_id: str, portfolio_b_id: str) -> CompetitionRun:
    run = CompetitionRun(
        id=uuid4().hex,
        portfolio_a_id=portfolio_a_id,
        portfolio_b_id=portfolio_b_id,
        name="Test Run",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _competition_decision(
    db,
    run_id: str,
    portfolio_id: str,
    round_number: int = 1,
    offset_hours: int = 0,
    winner: bool = False,
) -> CompetitionDecision:
    ts = NOW + timedelta(hours=offset_hours)
    cd = CompetitionDecision(
        id=uuid4().hex,
        run_id=run_id,
        portfolio_id=portfolio_id,
        round_number=round_number,
        council_result_json=json.dumps(
            {
                # Real CouncilResult shape: the decision lives in `decision`, not
                # top-level action/ticker/rationale keys.
                "decision": {
                    "trade_proposals": [
                        {"asset_id": "MSFT", "action": "sell", "quantity": 1.0, "rationale": "rebalance"}
                    ],
                    "strategy_narrative": "rebalance",
                    "confidence": 0.6,
                },
                "errors": [],
            }
        ),
        executed_trades_json=json.dumps([{"ticker": "MSFT", "action": "sell"}]),
        score_json=json.dumps({"total": 0.85}),
        winner=winner,
        created_at=ts,
    )
    db.add(cd)
    db.commit()
    db.refresh(cd)
    return cd


# ---------------------------------------------------------------------------
# Import the handler under test
# ---------------------------------------------------------------------------

from app.interface.api.llm_portfolio import get_journal  # noqa: E402 — after DB setup

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestJournalMerge:
    def test_review_only_returns_source_review(self):
        db = _memory_db()
        user = _user(db)
        portfolio = _portfolio(db, user.id)
        _review_decision(db, portfolio.id, mandate="A", offset_hours=0)

        result = get_journal.__wrapped__(portfolio.id, user=user, db=db)

        assert len(result) == 1
        assert result[0]["source"] == "review"
        assert result[0]["decision_json"]["action"] == "buy"
        # LLM recommendation outputs must carry the disclaimer flags.
        assert result[0]["estimate"] is True
        assert result[0]["not_financial_advice"] is True

    def test_competition_only_returns_source_competition(self):
        db = _memory_db()
        user = _user(db)
        portfolio_a = _portfolio(db, user.id, mandate="A")
        portfolio_b = _portfolio(db, user.id, mandate="B")
        run = _competition_run(db, portfolio_a.id, portfolio_b.id)
        _competition_decision(db, run.id, portfolio_a.id, round_number=1)

        result = get_journal.__wrapped__(portfolio_a.id, user=user, db=db)

        assert len(result) == 1
        assert result[0]["source"] == "competition"
        assert result[0]["decision_json"]["action"] == "sell"
        assert result[0]["decision_json"]["round_number"] == 1
        assert result[0]["estimate"] is True
        assert result[0]["not_financial_advice"] is True

    def test_merged_sorted_desc(self):
        """Review at T+2, competition at T+1, competition at T+0 → review first."""
        db = _memory_db()
        user = _user(db)
        portfolio_a = _portfolio(db, user.id, mandate="A")
        portfolio_b = _portfolio(db, user.id, mandate="B")
        run = _competition_run(db, portfolio_a.id, portfolio_b.id)

        _review_decision(db, portfolio_a.id, mandate="A", offset_hours=2)      # newest
        _competition_decision(db, run.id, portfolio_a.id, round_number=2, offset_hours=1)  # middle
        _competition_decision(db, run.id, portfolio_a.id, round_number=1, offset_hours=0)  # oldest

        result = get_journal.__wrapped__(portfolio_a.id, user=user, db=db)

        assert len(result) == 3
        sources = [r["source"] for r in result]
        assert sources == ["review", "competition", "competition"]
        # review_date should be strictly descending
        dates = [r["review_date"] for r in result]
        for i in range(len(dates) - 1):
            assert dates[i] > dates[i + 1], f"Not sorted DESC: {dates}"

    def test_competition_decisions_for_other_portfolio_not_included(self):
        """Competition decisions belonging to portfolio_b must NOT appear in portfolio_a's journal."""
        db = _memory_db()
        user = _user(db)
        portfolio_a = _portfolio(db, user.id, mandate="A")
        portfolio_b = _portfolio(db, user.id, mandate="B")
        run = _competition_run(db, portfolio_a.id, portfolio_b.id)

        _competition_decision(db, run.id, portfolio_a.id, round_number=1)
        _competition_decision(db, run.id, portfolio_b.id, round_number=1)

        result_a = get_journal.__wrapped__(portfolio_a.id, user=user, db=db)
        result_b = get_journal.__wrapped__(portfolio_b.id, user=user, db=db)

        assert len(result_a) == 1
        assert len(result_b) == 1

    def test_empty_returns_empty_list(self):
        db = _memory_db()
        user = _user(db)
        portfolio = _portfolio(db, user.id)

        result = get_journal.__wrapped__(portfolio.id, user=user, db=db)
        assert result == []

    def test_competition_decision_shape(self):
        """Verify competition entry exposes the expected fields in the right shape."""
        db = _memory_db()
        user = _user(db)
        portfolio_a = _portfolio(db, user.id, mandate="A")
        portfolio_b = _portfolio(db, user.id, mandate="B")
        run = _competition_run(db, portfolio_a.id, portfolio_b.id)
        _competition_decision(db, run.id, portfolio_a.id, round_number=3, winner=True)

        result = get_journal.__wrapped__(portfolio_a.id, user=user, db=db)

        entry = result[0]
        assert entry["source"] == "competition"
        assert entry["status"] == "winner"
        assert entry["mandate"] == "A"
        assert entry["context_token_estimate"] is None
        assert entry["reflection_json"] is None
        dj = entry["decision_json"]
        assert dj["round_number"] == 3
        assert dj["winner"] is True
        assert dj["action"] == "sell"
        assert dj["thesis"] == "rebalance"
        assert isinstance(dj["trades"], list)
        assert dj["trades"][0]["ticker"] == "MSFT"
