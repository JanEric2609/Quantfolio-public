from datetime import date
from decimal import Decimal

from conftest import _memory_db
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import Category, EnvelopeBudget, Expense, User
from app.foundation.auth import current_user
from app.foundation.envelope import envelope_rollover_balance, envelope_status, set_envelope_budget


def _user_and_category(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()
    return user, category


def test_set_envelope_budget_creates_then_updates():
    db = _memory_db()
    user, category = _user_and_category(db)

    row = set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("200"))
    assert row.budgeted_amount == Decimal("200")

    row = set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("250"))
    assert row.budgeted_amount == Decimal("250")
    assert db.query(EnvelopeBudget).filter(EnvelopeBudget.user_id == user.id).count() == 1


def test_envelope_status_reports_budgeted_spent_and_remaining():
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("200"))
    db.add(
        Expense(
            user_id=user.id,
            date=date(2026, 8, 5),
            amount=Decimal("30"),
            description="Groceries",
            category_id=category.id,
            source="manual",
        )
    )
    db.commit()

    status = envelope_status(db, user.id, 2026, 8)

    assert len(status) == 1
    assert status[0]["category_id"] == category.id
    assert status[0]["budgeted"] == 200.0
    assert status[0]["spent"] == 30.0
    assert status[0]["remaining"] == 170.0
    assert status[0]["overspent"] is False


def test_envelope_rollover_balance_accumulates_unspent_across_months():
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 6, Decimal("100"))
    set_envelope_budget(db, user.id, category.id, 2026, 7, Decimal("100"))
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))
    db.add_all(
        [
            Expense(user_id=user.id, date=date(2026, 6, 10), amount=Decimal("40"), description="June groceries", category_id=category.id, source="manual"),
            Expense(user_id=user.id, date=date(2026, 7, 10), amount=Decimal("60"), description="July groceries", category_id=category.id, source="manual"),
            Expense(user_id=user.id, date=date(2026, 8, 10), amount=Decimal("120"), description="August groceries", category_id=category.id, source="manual"),
        ]
    )
    db.commit()

    assert envelope_rollover_balance(db, user.id, category.id, 2026, 6) == Decimal("60")
    assert envelope_rollover_balance(db, user.id, category.id, 2026, 7) == Decimal("100")
    assert envelope_rollover_balance(db, user.id, category.id, 2026, 8) == Decimal("80")


def test_envelope_rollover_balance_reports_overspend_with_no_budget_ever_set():
    db = _memory_db()
    user, category = _user_and_category(db)
    db.add(
        Expense(
            user_id=user.id,
            date=date(2026, 8, 5),
            amount=Decimal("30"),
            description="Unbudgeted groceries",
            category_id=category.id,
            source="manual",
        )
    )
    db.commit()

    assert envelope_rollover_balance(db, user.id, category.id, 2026, 8) == Decimal("-30")

    status = envelope_status(db, user.id, 2026, 8)

    assert len(status) == 1
    assert status[0]["category_id"] == category.id
    assert status[0]["remaining"] == -30.0
    assert status[0]["overspent"] is True


def test_envelope_endpoints_round_trip():
    db = _memory_db()
    user, category = _user_and_category(db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        put_response = client.put(
            f"/api/budget/envelopes/{category.id}",
            json={"year": 2026, "month": 8, "budgeted_amount": "200"},
        )
        assert put_response.status_code == 200
        assert put_response.json()["budgeted_amount"] == 200.0

        get_response = client.get("/api/budget/envelopes", params={"year": 2026, "month": 8})
        assert get_response.status_code == 200
        body = get_response.json()
        assert body["money_to_budget"] == 0.0
        assert len(body["envelopes"]) == 1
        assert body["envelopes"][0]["budgeted"] == 200.0
        assert body["envelopes"][0]["goal"] is None
    finally:
        app.dependency_overrides.clear()


def test_envelope_endpoint_includes_money_to_budget_and_goal_progress():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    from datetime import date as date_type
    from decimal import Decimal as DecimalType

    from app.foundation.models.entities import IncomeSource

    goal_category = Category(
        user_id=user.id,
        name="Laptop Fund",
        type="expense",
        target_amount=DecimalType("1200"),
        target_date=date_type(2027, 1, 1),
    )
    db.add(goal_category)
    db.add(IncomeSource(user_id=user.id, name="BAföG", amount=DecimalType("450"), next_date=date_type(2026, 9, 1)))
    db.commit()
    set_envelope_budget(db, user.id, goal_category.id, 2026, 8, Decimal("200"))

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.get("/api/budget/envelopes", params={"year": 2026, "month": 8})
        assert response.status_code == 200
        body = response.json()
        assert body["money_to_budget"] == 450.0
        row = next(row for row in body["envelopes"] if row["category_id"] == goal_category.id)
        assert row["goal"]["target_amount"] == 1200.0
        assert row["goal"]["progress"] == 200.0
    finally:
        app.dependency_overrides.clear()
