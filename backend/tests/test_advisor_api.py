"""API tests for the advisor tracer view (PR1 G1).

Acceptance: the view's endpoint returns real data from one run_advisor_cycle
execution — trades with thesis + confidence, risk-gate blocks, and the
scorecard (pending until horizons mature).
"""
import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import LlmPortfolioDecision, PaperPortfolio, PaperTrade, User
from app.foundation.models.entities._core import now_utc
from app.decision.advisor.cycle import ADVISOR_MANDATE
from app.foundation.auth import current_user


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id

    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user_id)
    return TestClient(app), maker, user_id


def test_latest_cycle_empty_state():
    client, _maker, _user_id = _client()
    resp = client.get("/api/advisor/cycle/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["portfolio_id"] is None
    assert body["decision"] is None
    assert body["trades"] == []
    assert body["scorecard"] is None


def test_latest_cycle_returns_decision_trades_and_pending_scorecard():
    client, maker, user_id = _client()
    db = maker()
    portfolio = PaperPortfolio(
        id=uuid4().hex, user_id=user_id, name="Advisor Loop",
        mandate=ADVISOR_MANDATE, managed_by="llm",
    )
    db.add(portfolio)
    db.commit()
    db.add(
        PaperTrade(
            id=uuid4().hex, portfolio_id=portfolio.id, ticker="CAND", side="buy",
            quantity=10, price=40, value=400, confidence=0.65,
            rationale="Quant screen + MC upside.",
        )
    )
    db.add(
        LlmPortfolioDecision(
            id=uuid4().hex, portfolio_id=portfolio.id, review_date=now_utc(),
            mandate=ADVISOR_MANDATE, status="completed",
            decision_json=json.dumps({
                "decisions": [
                    {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
                     "thesis": "Quant screen + MC upside.", "confidence": 0.65},
                ],
                "blocked": [{"symbol": "RISKY", "reason": "cvar breach"}],
                "risk_envelope": {"var_95_daily": 0.01, "cvar_95_daily": 0.02, "max_drawdown": 0.05},
            }),
        )
    )
    db.commit()

    resp = client.get("/api/advisor/cycle/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["portfolio_id"] == portfolio.id
    assert body["decision"]["decisions"][0]["ticker"] == "CAND"
    assert body["decision"]["blocked"][0]["symbol"] == "RISKY"
    assert body["trades"][0]["rationale"] == "Quant screen + MC upside."
    assert body["trades"][0]["confidence"] == 0.65
    # Scorecard honestly pending — no fabricated values.
    assert body["scorecard"] is None
