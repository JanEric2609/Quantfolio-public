"""Reflection memory tests (PR2 group A).

Acceptance: a scored cycle produces persisted lessons; the next
``decide_trades`` call includes them in the assembled prompt payload (LLM
stubbed); the lesson cap/decay keeps memory bounded.
"""
import json
from datetime import date, timedelta
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import AdvisorScorecard, AdvisorStrategy, StrategyLesson, User
from app.decision.advisor.llm_decision import decide_trades
from app.decision.advisor.reflection import (
    MAX_ACTIVE_LESSONS,
    MAX_NEW_PER_CYCLE,
    RANK_DECAY,
    get_active_lessons,
    reflect_on_cycle,
)


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _strategy(db, user: User) -> AdvisorStrategy:
    strat = AdvisorStrategy(user_id=user.id, role="champion", config_json={})
    db.add(strat)
    db.commit()
    return strat


def _scorecard(db, user: User, portfolio_id: str | None = None) -> AdvisorScorecard:
    from app.foundation.models.entities import PaperPortfolio

    if portfolio_id is None:
        p = PaperPortfolio(user_id=user.id, name="sleeve", initial_cash=1000, mandate="advisor")
        db.add(p)
        db.commit()
        portfolio_id = p.id
    card = AdvisorScorecard(
        user_id=user.id,
        portfolio_id=portfolio_id,
        window_start=date.today() - timedelta(days=90),
        window_end=date.today(),
        n_predictions=10,
        n_resolved=8,
        sharpe=0.5,
        brier_avg=0.2,
        mz_slope=0.9,
        mz_r2=0.3,
        max_drawdown=0.05,
    )
    db.add(card)
    db.commit()
    return card


_LESSON_RESPONSE = json.dumps({
    "lessons": [
        {"lesson": "Momentum entries in EU midcaps worked; keep them.",
         "tags": {"signals": ["momentum"], "regime": "risk-on", "sectors": ["industrials"]}},
        {"lesson": "High-confidence tech buys underperformed; size them down.",
         "tags": {"signals": ["llm_conviction"], "sectors": ["tech"]}},
    ]
})


def test_reflection_persists_lessons_and_next_decision_sees_them():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)

    created = reflect_on_cycle(
        db, strat, card,
        [{"ticker": "AAA", "direction": "buy", "realised_return": 0.05}],
        llm_call=lambda _db, _msgs: _LESSON_RESPONSE,
    )
    db.commit()
    assert len(created) == 2
    rows = db.query(StrategyLesson).filter(StrategyLesson.strategy_id == strat.id).all()
    assert all(r.active and r.rank == 1.0 for r in rows)
    assert rows[0].source_scorecard_id == card.id

    # The next decision call must carry the lessons in the user prompt.
    lessons = get_active_lessons(db, strat.id)
    assert "Momentum entries in EU midcaps worked; keep them." in lessons

    captured: dict = {}

    def _capture_llm(_db, messages):
        captured["user"] = messages[1]["content"]
        return json.dumps({"decisions": [
            {"ticker": "AAA", "action": "hold", "target_weight": 0.0,
             "thesis": "", "confidence": 0.5},
        ]})

    from app.decision.advisor.decision import TradeProposal
    from app.decision.advisor.risk_gate import RiskGateResult

    proposal = TradeProposal(
        portfolio_id="p", suggested_weights={"AAA": 0.1}, mc_summaries={},
        risk_envelope={}, current_book={}, optimizer_status="ok",
    )
    gate = RiskGateResult(
        passed=True, envelope={}, ceilings={}, passing_symbols=["AAA"],
    )
    decide_trades(db, proposal, gate, lessons=lessons, llm_call=_capture_llm)
    payload = json.loads(captured["user"])
    assert lessons[0] in payload["lessons"]


def test_reflection_strips_sectors_when_no_equity_in_batch():
    """Prod incident (2026-08-20): XEON.DE, a money-market ETF with no
    sector data anywhere in the decisions payload, was tagged
    "sectors": ["technology"] by the reflection LLM — pure fabrication,
    since the payload gave it nothing to ground that guess in. Defense in
    depth: if nothing in the batch is instrument_type=='equity', strip any
    sectors tag regardless of what the LLM returned."""
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)

    created = reflect_on_cycle(
        db, strat, card,
        [{"ticker": "XEON.DE", "instrument_type": "money_market", "direction": "buy",
          "realised_return": 0.005}],
        llm_call=lambda _db, _msgs: _LESSON_RESPONSE,
    )
    db.commit()
    assert len(created) == 2
    for row in created:
        assert row.tags_json.get("sectors") in (None, [])


def test_reflection_keeps_sectors_when_equity_present():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)

    created = reflect_on_cycle(
        db, strat, card,
        [{"ticker": "AAPL", "instrument_type": "equity", "direction": "buy",
          "realised_return": 0.05}],
        llm_call=lambda _db, _msgs: _LESSON_RESPONSE,
    )
    db.commit()
    assert created[0].tags_json["sectors"] == ["industrials"]


def test_reflection_unusable_response_creates_no_lessons():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)
    created = reflect_on_cycle(db, strat, card, [], llm_call=lambda *_: "not json")
    assert created == []
    assert db.query(StrategyLesson).count() == 0


def test_lessons_cap_and_decay():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)

    # Reflect repeatedly: memory must stay bounded and ranks must decay.
    for i in range(6):
        response = json.dumps({
            "lessons": [{"lesson": f"cycle {i} lesson {j}", "tags": {}} for j in range(3)]
        })
        reflect_on_cycle(db, strat, card, [], llm_call=lambda *_a, _r=response: _r)
    db.commit()

    active = (
        db.query(StrategyLesson)
        .filter(StrategyLesson.strategy_id == strat.id, StrategyLesson.active.is_(True))
        .all()
    )
    assert len(active) <= MAX_ACTIVE_LESSONS
    # Newest lessons carry full rank; older active ones are decayed.
    ranks = sorted((r.rank for r in active), reverse=True)
    assert ranks[0] == 1.0
    assert any(r < 1.0 for r in ranks)
    # get_active_lessons respects the cap too.
    assert len(get_active_lessons(db, strat.id)) <= MAX_ACTIVE_LESSONS
    # Sanity on constants used above.
    assert MAX_NEW_PER_CYCLE == 3 and 0 < RANK_DECAY < 1
