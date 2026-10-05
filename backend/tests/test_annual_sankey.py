from datetime import date
from decimal import Decimal

from conftest import _memory_db
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import Category, Expense, IncomeSource, User
from app.foundation.auth import current_user
from app.foundation.budget_insights import annual_sankey_data


def test_annual_sankey_builds_income_expense_and_merchant_links():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    income_category = Category(user_id=user.id, name="Job", type="income")
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    db.add_all([income_category, groceries])
    db.commit()
    db.add(IncomeSource(user_id=user.id, name="BAföG", amount=Decimal("450"), next_date=date(2026, 9, 1)))
    db.commit()

    db.add_all(
        [
            Expense(
                user_id=user.id,
                date=date(2026, 3, 1),
                amount=Decimal("450"),
                description="BAföG payment",
                category_id=income_category.id,
                source="manual",
            ),
            Expense(
                user_id=user.id,
                date=date(2026, 3, 5),
                amount=Decimal("30"),
                description="Rewe",
                category_id=groceries.id,
                source="manual",
            ),
            Expense(
                user_id=user.id,
                date=date(2026, 3, 12),
                amount=Decimal("20"),
                description="Rewe",
                category_id=groceries.id,
                source="manual",
            ),
            Expense(
                user_id=user.id,
                date=date(2025, 12, 31),
                amount=Decimal("999"),
                description="Out of year range",
                category_id=groceries.id,
                source="manual",
            ),
        ]
    )
    db.commit()

    result = annual_sankey_data(db, user.id, 2026)

    assert result["year"] == 2026
    assert result["total_income"] == 450.0
    assert result["total_expense"] == 50.0

    income_links = [link for link in result["links"] if link["target"] == "Income"]
    assert income_links == [{"source": "BAföG", "target": "Income", "value": 450.0}]

    category_links = [link for link in result["links"] if link["source"] == "Income"]
    assert category_links == [{"source": "Income", "target": "Groceries", "value": 50.0}]

    merchant_links = [link for link in result["links"] if link["source"] == "Groceries"]
    assert merchant_links == [{"source": "Groceries", "target": "Rewe · Groceries", "value": 50.0}]


def test_annual_sankey_falls_back_to_category_name_without_income_source_match():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    income_category = Category(user_id=user.id, name="Freelance", type="income")
    db.add(income_category)
    db.commit()
    db.add(
        Expense(
            user_id=user.id,
            date=date(2026, 5, 1),
            amount=Decimal("300"),
            description="Client invoice",
            category_id=income_category.id,
            source="manual",
        )
    )
    db.commit()

    result = annual_sankey_data(db, user.id, 2026)

    assert {"source": "Freelance", "target": "Income", "value": 300.0} in result["links"]


def test_annual_sankey_endpoint_round_trip():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.get("/api/budget/reports/sankey", params={"year": 2026})
        assert response.status_code == 200
        body = response.json()
        assert body["year"] == 2026
        assert body["links"] == []
    finally:
        app.dependency_overrides.clear()
