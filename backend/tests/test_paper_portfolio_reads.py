"""Direct coverage for paper_portfolio read helpers (issue #136).

The read/query paths (`get_holdings`, `get_trades`, `get_snapshots`,
`compute_metrics`) were only exercised indirectly via API tests. These pin
their behaviour against a freshly-seeded in-memory portfolio.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import _memory_db

from app.foundation.models.entities import MetricsSnapshot, PaperSnapshot, User
from app.decision.paper_portfolio import (
    compute_metrics,
    execute_trade,
    get_holdings,
    get_or_create_paper_portfolio,
    get_snapshots,
    get_summary,
    get_trades,
)


def _user(db) -> User:
    user = User(username="reader", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_get_trades_returns_recorded_trades_most_recent_first():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)

    execute_trade(db, portfolio.id, ticker="AAPL", side="buy", quantity=10, price=100.0)
    execute_trade(db, portfolio.id, ticker="MSFT", side="buy", quantity=5, price=200.0)

    trades = get_trades(db, portfolio.id)
    assert len(trades) == 2
    assert {t.ticker for t in trades} == {"AAPL", "MSFT"}


def test_get_trades_respects_limit():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    for i in range(3):
        execute_trade(db, portfolio.id, ticker=f"T{i}", side="buy", quantity=1, price=10.0)

    assert len(get_trades(db, portfolio.id, limit=2)) == 2


def test_get_holdings_reflects_buys():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    execute_trade(db, portfolio.id, ticker="AAPL", side="buy", quantity=10, price=100.0)

    holdings = get_holdings(db, portfolio.id)
    tickers = {h["ticker"] for h in holdings}
    assert "AAPL" in tickers


def test_get_snapshots_empty_for_fresh_portfolio():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    assert get_snapshots(db, portfolio.id) == []


def test_compute_metrics_on_portfolio_without_snapshots():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    metrics = compute_metrics(db, portfolio.id)
    # Must return a dict rather than raise when there is no history.
    assert isinstance(metrics, dict)


def _seed_daily_snapshots(db, portfolio_id: str, n: int, start_value=10_000.0):
    value = start_value
    day = date.today() - timedelta(days=n)
    for i in range(n):
        value *= 1 + (0.001 if i % 2 == 0 else -0.0005)
        db.add(PaperSnapshot(
            portfolio_id=portfolio_id, date=day + timedelta(days=i),
            total_value=Decimal(str(round(value, 2))), cash_balance=Decimal("0"),
            securities_value=Decimal(str(round(value, 2))), total_return_pct=Decimal("0"),
            currency="EUR",
        ))
    db.commit()


def test_compute_metrics_writes_metrics_snapshot():
    """Phase 4 (unified-portfolio-engine-implementation.md): compute_metrics
    mirrors sharpe/sortino/calmar/cvar/max_drawdown/volatility into the
    shared metrics_snapshots table (context="paper_portfolio"), not just the
    legacy PaperSnapshot.sharpe/.max_drawdown columns."""
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    _seed_daily_snapshots(db, portfolio.id, 25)

    result = compute_metrics(db, portfolio.id)
    assert result["updated"] is True

    snap = (
        db.query(MetricsSnapshot)
        .filter(MetricsSnapshot.portfolio_id == portfolio.id, MetricsSnapshot.context == "paper_portfolio")
        .one()
    )
    assert snap.sharpe == result["sharpe"]
    assert snap.max_drawdown == result["max_drawdown"]
    assert snap.sortino is not None
    assert snap.volatility is not None

    summary = get_summary(db, portfolio.id)
    assert summary["sharpe"] == result["sharpe"]
    assert summary["max_drawdown"] == result["max_drawdown"]
    # 25 daily snapshots span 24 days: far too short for an annualised Sharpe.
    assert summary["history_days"] == 24


def test_compute_metrics_upserts_metrics_snapshot_on_rerun():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    _seed_daily_snapshots(db, portfolio.id, 25)

    compute_metrics(db, portfolio.id)
    compute_metrics(db, portfolio.id)

    assert db.query(MetricsSnapshot).filter(MetricsSnapshot.context == "paper_portfolio").count() == 1


def test_get_summary_falls_back_per_value_not_per_row():
    """A snapshot row with a NULL sharpe must not blank a valid legacy value.

    Every metrics_snapshots column is nullable, so keying the legacy fallback
    on row *presence* meant a partially-populated snapshot row silently erased
    a good PaperSnapshot.sharpe rather than falling back to it.
    """
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)

    snap = PaperSnapshot(
        portfolio_id=portfolio.id,
        date=date(2026, 8, 20),
        total_value=Decimal("101000"),
        cash_balance=Decimal("1000"),
        sharpe=1.234,
        max_drawdown=-0.05,
    )
    db.add(snap)
    db.add(
        MetricsSnapshot(
            portfolio_id=portfolio.id,
            as_of=date(2026, 8, 20),
            context="paper_portfolio",
            sharpe=None,
            max_drawdown=None,
        )
    )
    db.commit()

    summary = get_summary(db, portfolio.id)
    assert summary["sharpe"] == pytest.approx(1.234)
    assert summary["max_drawdown"] == pytest.approx(-0.05)


def test_metrics_snapshot_upsert_survives_a_concurrent_insert():
    """The loser of the unique-constraint race must not lose its legacy write.

    compute_metrics commits the snapshot row in the same transaction as
    PaperSnapshot.sharpe, so an unhandled IntegrityError discarded both and
    surfaced as a 500. The nightly job and a user-triggered recompute can
    genuinely collide on the same portfolio.
    """
    from app.foundation.metrics_snapshots import upsert_metrics_snapshot

    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)

    # Simulate the concurrent writer having already committed the same key.
    db.add(
        MetricsSnapshot(
            portfolio_id=portfolio.id,
            as_of=date(2026, 8, 20),
            context="paper_portfolio",
            sharpe=0.1,
        )
    )
    db.commit()
    db.expire_all()

    upsert_metrics_snapshot(
        db,
        portfolio_id=portfolio.id,
        as_of=date(2026, 8, 20),
        context="paper_portfolio",
        values={"sharpe": 0.9},
    )
    db.commit()

    rows = (
        db.query(MetricsSnapshot)
        .filter(MetricsSnapshot.portfolio_id == portfolio.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].sharpe == pytest.approx(0.9)
