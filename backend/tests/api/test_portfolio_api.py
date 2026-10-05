"""API tests for portfolio endpoints."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import Holding, Portfolio, User
from app.foundation.auth import current_user


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


@pytest.fixture
def client(db):
    from app.main import app

    user = User(username="null-avg-buy-api", password_hash="hash")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    yield TestClient(app, raise_server_exceptions=False), user
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(current_user, None)


def test_get_holdings_returns_null_avg_buy_price_when_unknown(client, db):
    tc, user = client
    portfolio = Portfolio(user_id=user.id, name="Main")
    db.add(portfolio)
    db.commit()
    db.add(
        Holding(
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            ticker="IWDA",
            name="iShares Core MSCI World",
            asset_type="etf",
            quantity=Decimal("120.5"),
            avg_buy_price=None,
            source="dkb_sync",
        )
    )
    db.commit()

    response = tc.get("/api/portfolio/holdings")
    assert response.status_code == 200
    body = response.json()
    holding = next(h for h in body if h["isin"] == "IE00B4L5Y983")
    assert holding["avg_buy_price"] is None
