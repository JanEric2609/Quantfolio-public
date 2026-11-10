"""Reflection memory tests (PR2 group A).

Acceptance: a scored cycle produces persisted lessons; the next
``decide_trades`` call includes them in its system prompt (LLM stubbed); the
lesson cap/decay keeps memory bounded; a lesson the computed record does not
back is never stored (2026-11-09 audit).
"""
import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorScorecard,
    AdvisorStrategy,
    DiscoveryPrediction,
    PaperPortfolio,
    StrategyLesson,
    User,
)
from app.decision.advisor.llm_decision import decide_trades
from app.decision.advisor.reflection import (
    MAX_ACTIVE_LESSONS,
    MAX_NEW_PER_CYCLE,
    RANK_DECAY,
    STAT_KEYS,
    get_active_lessons,
    lesson_rejection,
    reflect_on_cycle,
    track_record_stats,
)


def _group(n=24, hit=0.25, excess=-1.2, conf=0.7, brier=0.33):
    return {"n": n, "hit_rate": hit, "mean_excess_pct": excess, "mean_confidence": conf, "brier": brier}


def _stats(**overrides):
    groups = {key: _group() for key in STAT_KEYS}
    groups["buys"] = _group(n=22, hit=0.667, excess=1.5, conf=0.6, brier=0.2)
    groups.update(overrides)
    return {"groups": groups, "calibration": {"brier": 0.33, "coin_flip_brier": 0.25, "bins": []}}


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
         "stat": "buys", "verdict": "working",
         "tags": {"signals": ["momentum"], "regime": "risk-on", "sectors": ["industrials"]}},
        {"lesson": "High-confidence calls won only 25% of the time; state lower confidence.",
         "stat": "high_confidence", "verdict": "not_working",
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
        stats=_stats(),
    )
    db.commit()
    assert len(created) == 2
    rows = db.query(StrategyLesson).filter(StrategyLesson.strategy_id == strat.id).all()
    assert all(r.active and r.rank == 1.0 for r in rows)
    assert rows[0].source_scorecard_id == card.id
    assert {r.tags_json["stat"] for r in rows} == {"buys", "high_confidence"}

    # The next decision call must carry the lessons, with their evidence.
    lessons = get_active_lessons(db, strat.id)
    assert "[buys, n=22, working] Momentum entries in EU midcaps worked; keep them." in lessons

    captured: dict = {}

    def _capture_llm(_db, messages):
        captured["system"] = messages[0]["content"]
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
    decide_trades(db, proposal, gate, lessons=lessons, track_record=_stats(), llm_call=_capture_llm)
    assert lessons[0] in captured["system"]
    assert "Lessons from your own scored trades" in captured["system"]
    payload = json.loads(captured["user"])
    assert payload["your_track_record"]["groups"]["buys"]["n"] == 22


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
        stats=_stats(),
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
        stats=_stats(),
    )
    db.commit()
    assert created[0].tags_json["sectors"] == ["industrials"]


def test_reflection_unusable_response_creates_no_lessons():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)
    created = reflect_on_cycle(db, strat, card, [], llm_call=lambda *_: "not json", stats=_stats())
    assert created == []
    assert db.query(StrategyLesson).count() == 0


def test_lessons_cap_and_decay():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)

    # Reflect repeatedly: memory must stay bounded and ranks must decay.
    losing = [k for k in STAT_KEYS if k != "buys"]
    for i in range(6):
        response = json.dumps({
            "lessons": [
                {"lesson": f"cycle {i} lesson {j}", "stat": losing[(i + j) % len(losing)],
                 "verdict": "not_working", "tags": {}}
                for j in range(3)
            ]
        })
        reflect_on_cycle(db, strat, card, [], llm_call=lambda *_a, _r=response: _r, stats=_stats())
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


# ── verification against the computed record (2026-11-09 audit) ───────────


def test_lesson_quoting_a_false_percentage_is_rejected():
    """Prod lesson: "over 70% of buys gained" while buys had won 25%."""
    stats = _stats(buys=_group(n=24, hit=0.25, excess=-1.2))
    item = {"lesson": "Over 70% of buys gained; keep buying.", "stat": "buys", "verdict": "not_working"}
    assert "quotes 70%" in lesson_rejection(item, stats)
    item["lesson"] = "Only 25% of buys beat the index; buy less."
    assert lesson_rejection(item, stats) is None
    item["lesson"] = "Buys stated 70% confidence but won 25%; state less."
    assert lesson_rejection(item, stats) is None


def test_lesson_verdict_must_match_the_sign():
    stats = _stats(buys=_group(n=24, hit=0.25, excess=-1.2))
    item = {"lesson": "Buys are working well.", "stat": "buys", "verdict": "working"}
    assert "contradicts" in lesson_rejection(item, stats)


def test_lesson_on_a_thin_group_is_rejected():
    stats = _stats(sells=_group(n=3))
    item = {"lesson": "Sells are not working.", "stat": "sells", "verdict": "not_working"}
    assert "fewer than" in lesson_rejection(item, stats)


def test_lesson_on_a_coin_flip_record_is_rejected():
    stats = _stats(buys=_group(n=30, hit=0.533, excess=0.4))
    item = {"lesson": "Buys are working.", "stat": "buys", "verdict": "working"}
    assert "coin flip" in lesson_rejection(item, stats)


def test_no_reflection_before_enough_trades():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)
    calls = []
    created = reflect_on_cycle(
        db, strat, card, [], llm_call=lambda *_: calls.append(1) or _LESSON_RESPONSE,
        stats=_stats(all=_group(n=2)),
    )
    assert created == [] and calls == []


def test_newer_lesson_on_the_same_group_replaces_the_older_one():
    db = _memory_db()
    user = _user(db)
    strat = _strategy(db, user)
    card = _scorecard(db, user)
    first = json.dumps({"lessons": [{"lesson": "Sells lose money.", "stat": "sells", "verdict": "not_working"}]})
    second = json.dumps({"lessons": [{"lesson": "Sells keep losing; hold more.", "stat": "sells",
                                      "verdict": "not_working"}]})
    reflect_on_cycle(db, strat, card, [], llm_call=lambda *_: first, stats=_stats())
    reflect_on_cycle(db, strat, card, [], llm_call=lambda *_: second, stats=_stats())
    db.commit()
    active = get_active_lessons(db, strat.id)
    assert active == ["[sells, n=24, not_working] Sells keep losing; hold more."]


def test_unverified_and_pre_reset_lessons_are_not_shown():
    db = _memory_db()
    user = _user(db)
    p = PaperPortfolio(user_id=user.id, name="sleeve", initial_cash=1000, mandate="advisor",
                       inception_at=datetime.now(UTC) - timedelta(days=10))
    db.add(p)
    db.commit()
    strat = AdvisorStrategy(user_id=user.id, role="champion", config_json={}, portfolio_id=p.id)
    db.add(strat)
    db.commit()
    db.add_all([
        StrategyLesson(user_id=user.id, strategy_id=strat.id, lesson_text="legacy, no stat", tags_json={}),
        StrategyLesson(user_id=user.id, strategy_id=strat.id, lesson_text="old run",
                       tags_json={"stat": "buys", "verdict": "working", "n": 9},
                       created_at=datetime.now(UTC) - timedelta(days=30)),
        StrategyLesson(user_id=user.id, strategy_id=strat.id, lesson_text="current run",
                       tags_json={"stat": "sells", "verdict": "not_working", "n": 6}),
    ])
    db.commit()
    assert get_active_lessons(db, strat.id) == ["[sells, n=6, not_working] current run"]


def test_track_record_counts_only_the_current_run():
    db = _memory_db()
    user = _user(db)
    start = datetime.now(UTC) - timedelta(days=60)
    p = PaperPortfolio(user_id=user.id, name="sleeve", initial_cash=1000, mandate="advisor", inception_at=start)
    db.add(p)
    db.commit()

    def pred(days_ago, direction, conviction, excess):
        at = datetime.now(UTC) - timedelta(days=days_ago)
        return DiscoveryPrediction(
            user_id=user.id, run_id="r", symbol="AAPL", predicted_at=at, horizon_days=21,
            resolve_at=at + timedelta(days=30), direction=direction, conviction=conviction,
            realised_return=excess + 0.01, excess_return=excess, outcome_status="resolved",
            portfolio_id=p.id,
        )

    db.add_all([
        pred(50, "buy", 0.7, -0.02),
        pred(40, "buy", 0.7, 0.03),
        pred(30, "sell", 0.55, -0.01),
        pred(20, "neutral", 0.5, 0.01),   # a hold: no directional bet
        pred(90, "buy", 0.8, 0.10),       # before the reset
    ])
    db.commit()
    stats = track_record_stats(db, user.id, p.id)
    groups = stats["groups"]
    assert groups["all"]["n"] == 3
    assert groups["buys"]["n"] == 2 and groups["buys"]["hit_rate"] == 0.5
    assert groups["sells"]["n"] == 1
    assert groups["high_confidence"]["n"] == 2
    assert groups["all"]["mean_excess_pct"] == 0.0
    # Brier of stated confidence vs hit: ((0.7-0)^2 + (0.7-1)^2 + (0.55-0)^2) / 3
    assert abs(groups["all"]["brier"] - round((0.49 + 0.09 + 0.3025) / 3, 3)) < 1e-9
    assert stats["since"] == start.date().isoformat()
