from datetime import date
from decimal import Decimal

from conftest import _memory_db
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import Category, IncomeSource, User
from app.foundation.auth import current_user
from app.foundation.budget import money_to_budget, monthly_income_amount


def test_category_target_fields_default_to_none():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()
    db.refresh(category)

    assert category.target_amount is None
    assert category.target_date is None


def test_category_can_carry_goal_fields_regardless_of_type():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    category = Category(
        user_id=user.id,
        name="Laptop Fund",
        type="expense",
        target_amount=Decimal("1200"),
        target_date=date(2027, 1, 1),
    )
    db.add(category)
    db.commit()
    db.refresh(category)

    assert category.target_amount == Decimal("1200")
    assert category.target_date == date(2027, 1, 1)


def test_income_source_insert_and_defaults():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    source = IncomeSource(user_id=user.id, name="BAföG", amount=Decimal("450"), next_date=date(2026, 9, 1))
    db.add(source)
    db.commit()
    db.refresh(source)

    assert source.currency == "EUR"
    assert source.cadence == "monthly"
    assert source.active is True


def test_monthly_income_amount_weekly_cadence():
    source = IncomeSource(name="Side gig", amount=Decimal("50"), cadence="weekly", next_date=date(2026, 9, 1))
    assert monthly_income_amount(source) == Decimal("50") * Decimal("52") / Decimal("12")


def test_monthly_income_amount_quarterly_cadence():
    source = IncomeSource(name="Dividend", amount=Decimal("300"), cadence="quarterly", next_date=date(2026, 9, 1))
    assert monthly_income_amount(source) == Decimal("100")


def test_monthly_income_amount_annual_cadence():
    source = IncomeSource(name="Bonus", amount=Decimal("1200"), cadence="annual", next_date=date(2026, 9, 1))
    assert monthly_income_amount(source) == Decimal("100")


def test_monthly_income_amount_monthly_cadence_is_passthrough():
    source = IncomeSource(name="BAföG", amount=Decimal("450"), cadence="monthly", next_date=date(2026, 9, 1))
    assert monthly_income_amount(source) == Decimal("450")


def test_money_to_budget_sums_only_active_sources():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.add_all(
        [
            IncomeSource(user_id=user.id, name="BAföG", amount=Decimal("450"), cadence="monthly", next_date=date(2026, 9, 1)),
            IncomeSource(user_id=user.id, name="Parents", amount=Decimal("200"), cadence="monthly", next_date=date(2026, 9, 1)),
            IncomeSource(user_id=user.id, name="Old job", amount=Decimal("900"), cadence="monthly", next_date=date(2026, 9, 1), active=False),
        ]
    )
    db.commit()

    assert money_to_budget(db, user.id) == Decimal("650")


def test_money_to_budget_returns_zero_with_no_sources():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    assert money_to_budget(db, user.id) == Decimal("0")


def _client_for(db, user):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def test_income_source_crud_round_trip():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    client = _client_for(db, user)
    try:
        create_response = client.post(
            "/api/budget/income-sources",
            json={"name": "BAföG", "amount": "450", "cadence": "monthly", "next_date": "2026-09-01"},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["name"] == "BAföG"
        assert created["active"] is True

        list_response = client.get("/api/budget/income-sources")
        assert list_response.status_code == 200
        assert len(list_response.json()) == 1

        update_response = client.put(
            f"/api/budget/income-sources/{created['id']}",
            json={"name": "BAföG", "amount": "500", "cadence": "monthly", "next_date": "2026-09-01", "active": False},
        )
        assert update_response.status_code == 200
        assert update_response.json()["amount"] == 500.0
        assert update_response.json()["active"] is False

        delete_response = client.delete(f"/api/budget/income-sources/{created['id']}")
        assert delete_response.status_code == 200
        assert client.get("/api/budget/income-sources").json() == []
    finally:
        app.dependency_overrides.clear()


def test_income_source_scoped_to_owner():
    db = _memory_db()
    owner = User(username="jan", password_hash="hash")
    other = User(username="alex", password_hash="hash")
    db.add_all([owner, other])
    db.commit()
    source = IncomeSource(user_id=owner.id, name="Job", amount=Decimal("500"), next_date=date(2026, 9, 1))
    db.add(source)
    db.commit()

    client = _client_for(db, other)
    try:
        response = client.put(
            f"/api/budget/income-sources/{source.id}",
            json={"name": "Job", "amount": "999", "cadence": "monthly", "next_date": "2026-09-01"},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
