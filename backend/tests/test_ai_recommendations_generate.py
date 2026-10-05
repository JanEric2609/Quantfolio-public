"""Integration test for the consolidated /api/ai/recommendations/generate path.

Covers the full wiring: services.ai.generate_recommendations_for_user_async ->
recommendation_engine (context build, LLM call, evidence validation) ->
v2_adapter -> persist_recommendation_v2, with the LLM and market-data calls
mocked so the test is fast and deterministic.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import Recommendation, User


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _FakeCompletion:
    def __init__(self, content: str):
        self.content = content


@pytest.mark.asyncio
async def test_generate_recommendations_for_user_persists_v2_rows(monkeypatch):
    from app.decision import ai as ai_module
    from app.foundation import market as market_module
    import app.foundation.llm.router as llm_router_module

    db = _memory_db()
    user = User(username="gen-test", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)

    monkeypatch.setattr(market_module, "history", lambda *args, **kwargs: [])

    tickers = ["IWDA.AS", "EUNL.DE", "VWCE.DE", "SXR8.DE", "EXS1.DE"]

    async def _fake_call(db, task_type, messages, **kwargs):
        payload = [
            {
                "ticker": tickers[0],
                "action": "BUY",
                "confidence": "high",
                "bull_case": "Broad diversified core exposure.",
                "bear_case": "Global equity drawdown risk.",
                "horizon_months": 84,
                "thesis": "Solid long-term core holding.",
                "risks": ["Market-wide drawdown"],
            },
            {
                "ticker": tickers[1],
                "action": "AVOID",
                "confidence": "low",
                "bear_case": "Overlaps with an existing core position.",
                "thesis": "Redundant with another candidate.",
                "risks": ["Concentration overlap"],
            },
        ]
        return _FakeCompletion(json.dumps(payload))

    monkeypatch.setattr(llm_router_module, "call", _fake_call)

    result = await ai_module.generate_recommendations_for_user_async(
        db, user.id, universe="etfs_blue_chips", limit=5, mode="long_term"
    )

    created_tickers = {c["ticker"] for c in result["created"]}
    assert tickers[0] in created_tickers
    assert tickers[1] in created_tickers
    # The three candidates the fake LLM didn't return anything for are recorded
    # as failed, not silently dropped.
    failed_tickers = {f["ticker"] for f in result["failed"]}
    assert failed_tickers == set(tickers[2:])

    rows = db.query(Recommendation).filter(Recommendation.user_id == user.id).all()
    assert len(rows) == 2
    buy_row = next(r for r in rows if r.ticker == tickers[0])
    assert buy_row.verdict in {"BUY", "HOLD", "SELL", "AVOID", "WATCH"}  # legacy verdict, mapped from V2
    assert buy_row.recommendation_v2_payload_json is not None
    payload = json.loads(buy_row.recommendation_v2_payload_json)
    assert payload["bull_case"]
    assert payload["suggested_position_size"]["max_pct"] <= 100

    avoid_row = next(r for r in rows if r.ticker == tickers[1])
    avoid_payload = json.loads(avoid_row.recommendation_v2_payload_json)
    assert avoid_payload["verdict"] in {"AVOID", "REJECTED_BY_RISK"}
