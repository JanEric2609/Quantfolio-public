"""Time-weighted unit value of the real book: flows are never return."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import book_performance
from app.foundation.core.db import Base
from app.foundation.models.entities import BookPositionSnapshot, User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


D0 = date.today() - timedelta(days=10)


def _day(i: int) -> str:
    return (D0 + timedelta(days=i)).isoformat()


def _snap(db, user, i, qty, source="dkb"):
    db.add(BookPositionSnapshot(
        user_id=user.id, snapshot_date=D0 + timedelta(days=i), source=source, account_id="a1",
        isin="IE00B4L5Y983", ticker="EUNL.DE", name="MSCI World", quantity=Decimal(str(qty)), currency="EUR",
    ))


def _run(db, user, prices, bench=None):
    def closes(_db, symbol, days=730, **kw):
        return bench if symbol == "BENCH" else prices
    with patch("app.foundation.eur_prices.eur_closes", side_effect=closes):
        return book_performance.compute_book_performance(db, user.id, benchmark="BENCH")


def test_a_savings_plan_purchase_is_a_flow_not_return():
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    # 10 units, price 100 -> 110 -> 110; on day 2 ten more units are bought.
    _snap(db, user, 0, 10)
    _snap(db, user, 2, 20)
    db.commit()
    prices = {_day(0): 100.0, _day(1): 110.0, _day(2): 110.0}
    perf = _run(db, user, prices, bench={_day(0): 50.0, _day(1): 55.0, _day(2): 55.0})
    assert perf["available"]
    assert perf["twr"] == pytest.approx(0.10)  # only the 100 -> 110 move
    assert perf["value_end_eur"] == pytest.approx(2200.0)
    assert perf["net_contributions_eur"] == pytest.approx(1100.0)
    assert perf["benchmark_return"] == pytest.approx(0.10)
    # Naive value growth would read 2200 / 1000 - 1 = 120 %.
    assert perf["series"][-1]["net_flow"] == pytest.approx(1100.0)


def test_irr_matches_the_flows():
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    assert book_performance.xirr(flows) == pytest.approx(0.10, abs=1e-9)
    assert book_performance.xirr([(date(2025, 1, 1), -1.0)]) is None


def test_no_snapshots_is_unavailable():
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    perf = book_performance.compute_book_performance(db, user.id)
    assert perf["available"] is False and "snapshot" in perf["reason"]


def test_two_brokers_sum_per_isin():
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    _snap(db, user, 0, 10, source="dkb")
    _snap(db, user, 0, 5, source="scalable")
    db.commit()
    perf = _run(db, user, {_day(0): 100.0, _day(1): 90.0}, bench={})
    assert perf["value_start_eur"] == pytest.approx(1500.0)
    assert perf["twr"] == pytest.approx(-0.10)
