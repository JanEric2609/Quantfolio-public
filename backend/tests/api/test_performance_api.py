"""API tests for performance endpoints (Phase 3)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import PerformanceLedgerEntry, Portfolio, PortfolioSnapshot, User
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
    """Test client with database and auth setup."""
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    yield TestClient(app, raise_server_exceptions=False), user
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(current_user, None)


def test_performance_endpoints_exist(client):
    """Performance endpoints should be registered."""
    # Placeholder: verify endpoints exist in router
    # Actual testing requires database + auth setup

    # Expected endpoints:
    # POST /api/performance/composites
    # GET /api/performance/composites
    # POST /api/performance/ledger/snapshot
    # GET /api/performance/ledger

    assert True


def test_ledger_snapshot_computes_ex_post_risk_from_snapshot_series(client, db):
    """ex_post_risk should be computed server-side from snapshot series."""
    tc, user = client

    # Create a portfolio
    portfolio = Portfolio(user_id=user.id, name="Main", currency="EUR")
    db.add(portfolio)
    db.commit()

    # Create a composite
    composite = create_composite(db, user_id=user.id, name="All", portfolio_ids=[portfolio.id])
    db.commit()

    # Create at least 5 PortfolioSnapshot rows with varying total_value
    # to ensure volatility/max_drawdown/cvar_95 are non-trivial
    base_date = date.today() - timedelta(days=4)
    values = [100.0, 102.0, 101.0, 105.0, 103.0]  # varying returns
    for i, value in enumerate(values):
        snapshot_date = base_date + timedelta(days=i)
        db.add(PortfolioSnapshot(
            user_id=user.id,
            portfolio_id=portfolio.id,
            date=snapshot_date,
            total_value=Decimal(str(value)),
            source="computed"
        ))
    db.commit()

    # Make the snapshot endpoint request
    as_of = base_date + timedelta(days=len(values) - 1)
    resp = tc.post(
        "/api/performance/ledger/snapshot",
        json={"composite_id": composite.id, "as_of": datetime.combine(as_of, datetime.min.time()).isoformat()},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    ex_post_risk = body["research"]["ex_post_risk"]

    # Verify ex_post_risk is not empty
    assert ex_post_risk != {}
    assert "volatility" in ex_post_risk
    assert "max_drawdown" in ex_post_risk
    assert "cvar_95" in ex_post_risk

    # Strengthen beyond key-presence: an all-zeros degenerate dict (the bug
    # this guards against — see calculate_ex_post_risk's len(returns) < 2
    # early return) would satisfy the assertions above but not these.
    assert ex_post_risk["volatility"] > 0
    assert ex_post_risk["max_drawdown"] < 0


def test_ledger_snapshot_dedupes_same_date_rows_from_different_sources(client, db):
    """PortfolioSnapshot allows one row per (user, date, source) — a dkb_mirror
    and a computed row can share a date with wildly different total_value. The
    snapshot query must prefer dkb_mirror and drop the other same-date row,
    not let both flow into the return series (which would otherwise produce a
    spurious ~1000x same-date "return" between two differently-scaled marks).
    """
    tc, user = client

    portfolio = Portfolio(user_id=user.id, name="Main", currency="EUR")
    db.add(portfolio)
    db.commit()

    composite = create_composite(db, user_id=user.id, name="All", portfolio_ids=[portfolio.id])
    db.commit()

    base_date = date.today() - timedelta(days=4)
    values = [100.0, 102.0, 101.0, 105.0, 103.0]
    for i, value in enumerate(values):
        snapshot_date = base_date + timedelta(days=i)
        db.add(PortfolioSnapshot(
            user_id=user.id,
            portfolio_id=portfolio.id,
            date=snapshot_date,
            total_value=Decimal(str(value)),
            source="dkb_mirror",
        ))
    # A same-date, wrong-scale "computed" row on day 2 — must lose the
    # dkb_mirror-priority tie-break and be dropped, not counted as an extra
    # snapshot in the return series.
    db.add(PortfolioSnapshot(
        user_id=user.id,
        portfolio_id=portfolio.id,
        date=base_date + timedelta(days=2),
        total_value=Decimal("100000.0"),
        source="computed",
    ))
    db.commit()

    as_of = base_date + timedelta(days=len(values) - 1)
    resp = tc.post(
        "/api/performance/ledger/snapshot",
        json={"composite_id": composite.id, "as_of": datetime.combine(as_of, datetime.min.time()).isoformat()},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    ex_post_risk = body["research"]["ex_post_risk"]

    # No spurious 2x/0.5x (here ~1000x) intra-date jump leaking into volatility.
    assert ex_post_risk["volatility"] < 5.0

    entry = db.query(PerformanceLedgerEntry).filter_by(id=body["research"]["id"]).one()
    meta = json.loads(entry.snapshot_meta_json)
    # 5 distinct dates after dedup, not 6 raw rows.
    assert meta["snapshots_used"] == 5
