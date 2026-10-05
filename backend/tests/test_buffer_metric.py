from datetime import date
from decimal import Decimal

from conftest import _memory_db
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import Category, Expense, User
from app.foundation.auth import current_user
from app.foundation.budget_insights import buffer_metric
from app.foundation.envelope import set_envelope_budget


def test_buffer_metric_known_scenario():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()

    as_of = date(2026, 8, 20)
    # €100 budgeted for August with nothing spent yet this month builds a €100 buffer.
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))
    # €90 spent in July (before the first budgeted month, so it doesn't touch the
    # rollover balance) still falls inside the trailing 90-day window -> avg daily
    # spend of exactly €1.
    db.add(
        Expense(
            user_id=user.id,
            date=date(2026, 7, 1),
            amount=Decimal("90"),
            description="Groceries run",
            category_id=category.id,
            source="manual",
        )
    )
    db.commit()

    result = buffer_metric(db, user.id, as_of=as_of, lookback_days=90)

    assert result["buffer_amount"] == 100.0
    assert result["avg_daily_spend"] == 1.0
    assert result["buffer_days"] == 100.0


def test_buffer_metric_excludes_sinking_fund_categories():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    sinking_fund = Category(
        user_id=user.id,
        name="Laptop Fund",
        type="expense",
        target_amount=Decimal("1200"),
        target_date=date(2027, 1, 1),
    )
    db.add(sinking_fund)
    db.commit()

    as_of = date(2026, 8, 20)
    set_envelope_budget(db, user.id, sinking_fund.id, 2026, 8, Decimal("500"))

    result = buffer_metric(db, user.id, as_of=as_of, lookback_days=90)

    assert result["buffer_amount"] == 0.0


def test_buffer_metric_returns_none_days_with_zero_spend():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))

    result = buffer_metric(db, user.id, as_of=date(2026, 8, 20), lookback_days=90)

    assert result["buffer_amount"] == 100.0
    assert result["avg_daily_spend"] == 0.0
    assert result["buffer_days"] is None


def test_buffer_endpoint_round_trip():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.get("/api/budget/buffer")
        assert response.status_code == 200
        body = response.json()
        assert body["buffer_amount"] == 0.0
        assert body["buffer_days"] is None
    finally:
        app.dependency_overrides.clear()
