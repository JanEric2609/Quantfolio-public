"""Council wiring into run_mandate_review — advisory-only, decision_json unchanged.

Regression guard for Phase A of the council-wiring plan: the bounded LLM
tool-loop must remain the sole author of decision_json/gates/trades, and a
council failure must never block the review — only reflection_json["council"]
may change.
"""
from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import LlmPortfolioDecision, User


def _user(db) -> User:
    user = User(id=uuid4().hex, username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


_HOLD_RESPONSE = json.dumps({
    "decision": {
        "action": "hold",
        "ticker": "",
        "quantity": 0,
        "thesis": "Nothing compelling this week.",
        "alternatives_considered": [],
        "key_risks": [],
        "expectation": {
            "metric": "abs_return_pct", "direction": "increase",
            "magnitude": 0.0, "horizon_weeks": 4, "confidence": 0.5,
        },
    }
})

_FAKE_COUNCIL_RESULT = {
    "portfolio_id": "irrelevant",
    "analysis": {"opportunities": [], "risk_flags": [], "market_narrative": "calm", "confidence": 0.5},
    "risk": {"var_95": 0.01, "cvar_95": 0.015, "max_drawdown": 0.05},
    "macro": {"regime": "expansion", "risk_stance": "neutral"},
    "decision": {
        "trade_proposals": [{"asset_id": "AAA", "action": "buy", "quantity": 1.0}],
        "target_weights": {"AAA": 1.0},
        "strategy_narrative": "Committee favours AAA.",
        "confidence": 0.7,
    },
    "errors": [],
}


def test_council_advisory_context_does_not_change_decision_shape():
    """Same decision_json shape whether the council succeeds or fails outright."""
    from app.decision.llm_portfolio import review as review_mod

    with patch.object(review_mod, "_local_llm_sync", return_value=_HOLD_RESPONSE):
        # Council succeeds.
        db = _memory_db()
        user = _user(db)
        with patch.object(review_mod, "_run_council_advisory", return_value=_FAKE_COUNCIL_RESULT):
            result_with_council = review_mod.run_mandate_review(db, user.id, "A")

        # Council fails outright (returns None, as it does on any exception).
        db2 = _memory_db()
        user2 = _user(db2)
        with patch.object(review_mod, "_run_council_advisory", return_value=None):
            result_without_council = review_mod.run_mandate_review(db2, user2.id, "A")

    assert result_with_council["status"] == "completed"
    assert result_without_council["status"] == "completed"
    assert set(result_with_council["decision"].keys()) == set(result_without_council["decision"].keys())
    assert result_with_council["decision"]["action"] == "hold"
    assert result_without_council["decision"]["action"] == "hold"

    row_with = db.query(LlmPortfolioDecision).one()
    row_without = db2.query(LlmPortfolioDecision).one()
    assert json.loads(row_with.decision_json).keys() == json.loads(row_without.decision_json).keys()


def test_council_result_persisted_under_reflection_json_council_key():
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)
    with patch.object(review_mod, "_local_llm_sync", return_value=_HOLD_RESPONSE):
        with patch.object(review_mod, "_run_council_advisory", return_value=_FAKE_COUNCIL_RESULT):
            review_mod.run_mandate_review(db, user.id, "A")

    row = db.query(LlmPortfolioDecision).one()
    reflection = json.loads(row.reflection_json)
    assert reflection["council"] == _FAKE_COUNCIL_RESULT
    # Pre-existing keys stay put — additive, not reshaped (oasdiff-safe).
    assert "tool_results" in reflection
    assert "gate_result" in reflection
    assert "trade_created" in reflection


def test_council_result_none_when_council_fails():
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)
    with patch.object(review_mod, "_local_llm_sync", return_value=_HOLD_RESPONSE):
        with patch.object(review_mod, "_run_council_advisory", return_value=None):
            review_mod.run_mandate_review(db, user.id, "A")

    row = db.query(LlmPortfolioDecision).one()
    reflection = json.loads(row.reflection_json)
    assert reflection["council"] is None
