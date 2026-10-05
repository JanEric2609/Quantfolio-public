from datetime import date
from decimal import Decimal

from conftest import _memory_db
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import Category, Expense, User
from app.foundation.auth import current_user
from app.foundation.budget_insights import envelope_history
from app.foundation.envelope import set_envelope_budget


def _user_and_category(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()
    return user, category


def test_envelope_history_flags_consistently_over_category():
    db = _memory_db()
    user, category = _user_and_category(db)
    for month in (5, 6, 7, 8):
        set_envelope_budget(db, user.id, category.id, 2026, month, Decimal("100"))
        db.add(
            Expense(
                user_id=user.id,
                date=date(2026, month, 15),
                amount=Decimal("150"),
                description="Groceries",
                category_id=category.id,
                source="manual",
            )
        )
    db.commit()

    rows = envelope_history(db, user.id, months=4, as_of=date(2026, 8, 20))

    assert len(rows) == 1
    row = rows[0]
    assert row["category_id"] == category.id
    assert row["months_with_budget"] == 4
    assert row["months_over_budget"] == 4
    assert row["consistently_over"] is True
    assert row["consistently_under"] is False
    assert row["months"][-1] == {"year": 2026, "month": 8, "budgeted": 100.0, "spent": 150.0, "over": 50.0}


def test_envelope_history_flags_consistently_under_category():
    db = _memory_db()
    user, category = _user_and_category(db)
    for month in (5, 6, 7, 8):
        set_envelope_budget(db, user.id, category.id, 2026, month, Decimal("100"))
        db.add(
            Expense(
                user_id=user.id,
                date=date(2026, month, 15),
                amount=Decimal("40"),
                description="Groceries",
                category_id=category.id,
                source="manual",
            )
        )
    db.commit()

    rows = envelope_history(db, user.id, months=4, as_of=date(2026, 8, 20))

    row = rows[0]
    assert row["months_over_budget"] == 0
    assert row["consistently_under"] is True
    assert row["consistently_over"] is False


def test_envelope_history_with_no_budget_ever_set_is_not_flagged():
    db = _memory_db()
    user, category = _user_and_category(db)
    db.add(
        Expense(
            user_id=user.id,
            date=date(2026, 8, 10),
            amount=Decimal("40"),
            description="Groceries",
            category_id=category.id,
            source="manual",
        )
    )
    db.commit()

    rows = envelope_history(db, user.id, months=4, as_of=date(2026, 8, 20))

    row = rows[0]
    assert row["months_with_budget"] == 0
    assert row["consistently_over"] is False
    assert row["consistently_under"] is False


def test_envelope_history_endpoint_round_trip():
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.get("/api/budget/envelopes/history", params={"months": 3})
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["category_id"] == category.id
    finally:
        app.dependency_overrides.clear()
