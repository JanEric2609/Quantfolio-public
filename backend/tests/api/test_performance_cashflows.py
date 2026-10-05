"""Regression test: TWR/MWR must use external cashflows, not internal trades.

Previously the ledger-snapshot endpoint fed every security buy/sell (TransactionLog)
in as an external cashflow against total-wealth snapshots, so a plain purchase
booked as a large 'performance' loss. Internal trades must now be ignored; only
activity-ledger cashflow entries (deposits/withdrawals) count.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import (
    Holding,
    Portfolio,
    PortfolioSnapshot,
    TransactionLog,
    User,
)
from app.foundation.auth import current_user
from app.lab.performance_ledger import create_composite


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

    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    yield TestClient(app, raise_server_exceptions=False), user
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(current_user, None)


def test_internal_trades_do_not_distort_twr(client, db):
    tc, user = client
    d1 = date.today() - timedelta(days=2)
    d2 = date.today()

    portfolio = Portfolio(user_id=user.id, name="Main", currency="EUR")
    db.add(portfolio)
    db.commit()

    composite = create_composite(db, user_id=user.id, name="All", portfolio_ids=[portfolio.id])
    db.commit()

    # Total wealth rose 100 -> 110 (a clean +10% with no external money movement).
    db.add(PortfolioSnapshot(user_id=user.id, portfolio_id=portfolio.id, date=d1, total_value=Decimal("100"), source="computed"))
    db.add(PortfolioSnapshot(user_id=user.id, portfolio_id=portfolio.id, date=d2, total_value=Decimal("110"), source="computed"))

    # An internal security purchase — cash to equity, net wealth unchanged. Under
    # the old code this €50 buy would have been booked as an external contribution
    # and driven TWR sharply negative.
    holding = Holding(portfolio_id=portfolio.id, name="ETF", asset_type="etf", quantity=Decimal("1"), avg_buy_price=Decimal("50"), currency="EUR")
    db.add(holding)
    db.commit()
    db.add(TransactionLog(holding_id=holding.id, type="buy", date=d2, quantity=Decimal("1"), price=Decimal("50")))
    db.commit()

    resp = tc.post(
        "/api/performance/ledger/snapshot",
        json={"composite_id": composite.id, "as_of": datetime.combine(d2, datetime.min.time()).isoformat()},
    )

    assert resp.status_code == 200, resp.text
    twr = float(resp.json()["research"]["twr"])
    # Clean +10% from the snapshots, undistorted by the internal trade.
    assert twr == pytest.approx(0.10, abs=1e-6)
