"""Portfolio -> Performance "vs benchmark": the ledger TWR beside the passive core ETF."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification.benchmark import benchmark_comparison, passive_core_symbol, window_return
from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import Portfolio, PortfolioSnapshot, User
from app.foundation.settings import upsert_public_settings
from app.lab.performance_ledger import create_composite, write_ledger_snapshot
from app.main import app

TODAY = date.today()


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def history(monkeypatch):
    """Provider stub: ``history({"EUNL.DE": {date: close}})``."""
    store: dict[str, dict[date, float]] = {}

    def fake(db, ticker, days=365, allow_live=True):
        return [{"date": d, "close": c} for d, c in sorted(store.get(ticker.upper(), {}).items())]

    monkeypatch.setattr("app.decision.verification.benchmark.market.history", fake)
    return store


def _closes(start: date, values: list[float]) -> dict[date, float]:
    return {start + timedelta(days=i): v for i, v in enumerate(values)}


def test_default_symbol_and_setting_override():
    db = _memory_db()
    assert passive_core_symbol(db) == "EUNL.DE"
    upsert_public_settings(db, {"passive_core_ticker": "vwce.de"})
    assert passive_core_symbol(db) == "VWCE.DE"


def test_window_return_uses_the_last_close_on_or_before_each_edge(history):
    start = TODAY - timedelta(days=40)
    history["EUNL.DE"] = _closes(start - timedelta(days=5), [100 + i for i in range(60)])
    # start edge: close of start day (index 5 -> 105); end edge: close of end day.
    end = start + timedelta(days=20)

    result = window_return(_memory_db(), "EUNL.DE", start, end)

    assert result["return"] == pytest.approx(125 / 105 - 1)
    assert result["start"] == start.isoformat() and result["end"] == end.isoformat()


def test_window_return_steps_back_over_a_gap_but_not_past_a_week(history):
    start = TODAY - timedelta(days=30)
    end = TODAY - timedelta(days=5)
    # Closes exist only before the start (gap 2 days) and at the end.
    history["EUNL.DE"] = {start - timedelta(days=2): 100.0, end: 110.0}
    result = window_return(_memory_db(), "EUNL.DE", start, end)
    assert result["return"] == pytest.approx(0.10)
    assert result["start"] == (start - timedelta(days=2)).isoformat()

    history["EUNL.DE"] = {start - timedelta(days=9): 100.0, end: 110.0}
    assert window_return(_memory_db(), "EUNL.DE", start, end)["return"] is None


def test_window_return_none_without_history_or_for_an_empty_window(history):
    db = _memory_db()
    assert window_return(db, "EUNL.DE", TODAY - timedelta(days=30), TODAY)["return"] is None
    history["EUNL.DE"] = _closes(TODAY - timedelta(days=60), [100.0] * 61)
    assert window_return(db, "EUNL.DE", TODAY, TODAY)["return"] is None
    assert window_return(db, "EUNL.DE", TODAY, TODAY - timedelta(days=3))["return"] is None


def test_comparison_reports_excess_short_window_and_missing_data_message(history):
    db = _memory_db()
    start = TODAY - timedelta(days=100)
    history["EUNL.DE"] = _closes(start, [100 + 0.1 * i for i in range(101)])  # +10 % over 100 days

    result = benchmark_comparison(db, period_start=start, period_end=TODAY, portfolio_twr=0.04)

    assert result["benchmark_symbol"] == "EUNL.DE"
    assert result["benchmark_return"] == pytest.approx(0.10, abs=1e-6)
    assert result["excess"] == pytest.approx(0.04 - 0.10, abs=1e-6)
    assert result["short_window"] is True and result["period_days"] == 100
    assert result["message"] is None

    history.clear()
    empty = benchmark_comparison(db, period_start=start, period_end=TODAY, portfolio_twr=0.04)
    assert empty["benchmark_return"] is None and empty["excess"] is None
    assert "EUNL.DE" in empty["message"]

    long_ago = benchmark_comparison(db, period_start=TODAY - timedelta(days=500), period_end=TODAY, portfolio_twr=None)
    assert long_ago["short_window"] is False and long_ago["excess"] is None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(history):
    db = _memory_db()
    user = User(username="perf", password_hash="x")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        yield TestClient(app), db, user
    finally:
        app.dependency_overrides.clear()


def _composite(db, user) -> str:
    pf = Portfolio(user_id=user.id, name="Main", currency="EUR")
    db.add(pf)
    db.commit()
    return create_composite(db, user_id=user.id, name="All accounts", portfolio_ids=[pf.id]).id


def test_endpoint_compares_latest_twr_with_the_benchmark_since_the_first_snapshot(client, history):
    http, db, user = client
    composite_id = _composite(db, user)
    first = TODAY - timedelta(days=90)
    for i in range(3):
        db.add(PortfolioSnapshot(
            id=str(uuid4()), user_id=user.id, date=first + timedelta(days=i), total_value=Decimal(1000 + i),
            cash_value=Decimal(0), security_value=Decimal(1000 + i),
        ))
    write_ledger_snapshot(db, composite_id, datetime.now(UTC), twr=0.03, mwr=0.02)
    db.commit()
    history["EUNL.DE"] = _closes(first - timedelta(days=3), [100 + 0.1 * i for i in range(100)])

    body = http.get(f"/api/performance/ledger/{composite_id}/benchmark").json()["research"]

    assert body["composite_id"] == composite_id
    assert body["benchmark_symbol"] == "EUNL.DE"
    assert body["period_start"] == first.isoformat()
    assert body["portfolio_twr"] == 0.03
    assert body["benchmark_return"] is not None
    assert body["excess"] == pytest.approx(0.03 - body["benchmark_return"])
    assert body["short_window"] is True


def test_endpoint_404s_without_entries_snapshots_or_ownership(client):
    http, db, user = client
    composite_id = _composite(db, user)
    assert http.get(f"/api/performance/ledger/{composite_id}/benchmark").status_code == 404  # no entry
    write_ledger_snapshot(db, composite_id, datetime.now(UTC), twr=0.0, mwr=0.0)
    db.commit()
    assert http.get(f"/api/performance/ledger/{composite_id}/benchmark").status_code == 404  # no snapshots

    other = User(username="other", password_hash="x")
    db.add(other)
    db.commit()
    pf = Portfolio(user_id=other.id, name="Theirs", currency="EUR")
    db.add(pf)
    db.commit()
    theirs = create_composite(db, user_id=other.id, name="Theirs", portfolio_ids=[pf.id]).id
    assert http.get(f"/api/performance/ledger/{theirs}/benchmark").status_code == 404
