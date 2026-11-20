"""Paper sleeves: benchmark base on a weekend, max drawdown on every snapshot (plan 2026-10-12, WP10 L1/L2)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from conftest import _memory_db

from app.decision import paper_portfolio as pp
from app.foundation.models.entities import PaperPortfolio, PaperSnapshot, User


def test_weekend_reset_takes_the_last_close_as_benchmark_base():
    db = _memory_db()
    today = pp.now_utc().date()
    friday = (today - timedelta(days=1)).isoformat()
    with patch.object(pp, "paper_quote_eur", return_value={"price": 100.0, "stale": True}), \
            patch("app.foundation.eur_prices.eur_closes", return_value={friday: 95.5}):
        price, _at = pp._benchmark_base_quote(db)
    assert price == 95.5


def test_no_recent_close_leaves_the_base_empty():
    db = _memory_db()
    old = (pp.now_utc().date() - timedelta(days=12)).isoformat()
    with patch.object(pp, "paper_quote_eur", return_value={"price": None}), \
            patch("app.foundation.eur_prices.eur_closes", return_value={old: 95.5}):
        assert pp._benchmark_base_quote(db)[0] is None


def test_max_drawdown_is_set_on_each_snapshot():
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    p = PaperPortfolio(user_id=user.id, name="A", initial_cash=Decimal("10000"))
    db.add(p)
    db.commit()
    start = date(2026, 10, 1)
    for i, v in enumerate([10_000, 10_500, 9_450, 9_800]):
        db.add(PaperSnapshot(portfolio_id=p.id, date=start + timedelta(days=i), total_value=Decimal(v),
                             cash_balance=Decimal(v), securities_value=Decimal(0), total_return_pct=Decimal(0)))
    db.commit()
    pp.snapshot_paper_portfolio(db, p.id, as_of=start + timedelta(days=4))
    snap = db.query(PaperSnapshot).filter(PaperSnapshot.date == start + timedelta(days=4)).one()
    # Peak 10,500, trough 9,450: -10 %.
    assert snap.max_drawdown == pytest.approx(-0.10, abs=1e-6)
