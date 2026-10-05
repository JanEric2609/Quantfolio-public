from decimal import Decimal

import pytest
from conftest import _memory_db
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import Category, EnvelopeBudget, User
from app.foundation.auth import current_user
from app.foundation.envelope import cover_overspend, envelope_rollover_balance, set_envelope_budget


def test_cover_overspend_moves_budgeted_amount_between_categories():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    travel = Category(user_id=user.id, name="Travel", type="expense")
    db.add_all([groceries, travel])
    db.commit()
    set_envelope_budget(db, user.id, groceries.id, 2026, 8, Decimal("100"))
    set_envelope_budget(db, user.id, travel.id, 2026, 8, Decimal("50"))

    result = cover_overspend(db, user.id, travel.id, groceries.id, Decimal("20"), 2026, 8)

    assert result["from_category_id"] == travel.id
    assert result["to_category_id"] == groceries.id
    assert result["from_remaining_budgeted"] == 30.0
    assert result["to_remaining_budgeted"] == 120.0
    assert envelope_rollover_balance(db, user.id, groceries.id, 2026, 8) == Decimal("120")
    assert envelope_rollover_balance(db, user.id, travel.id, 2026, 8) == Decimal("30")


def test_cover_endpoint_round_trip():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    travel = Category(user_id=user.id, name="Travel", type="expense")
    db.add_all([groceries, travel])
    db.commit()
    set_envelope_budget(db, user.id, groceries.id, 2026, 8, Decimal("100"))
    set_envelope_budget(db, user.id, travel.id, 2026, 8, Decimal("50"))

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.post(
            "/api/budget/envelopes/cover",
            json={
                "from_category_id": travel.id,
                "to_category_id": groceries.id,
                "amount": "20",
                "year": 2026,
                "month": 8,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["from_remaining_budgeted"] == 30.0
        assert body["to_remaining_budgeted"] == 120.0
    finally:
        app.dependency_overrides.clear()


def _budgeted(db, user_id, category_id, year, month) -> Decimal:
    row = (
        db.query(EnvelopeBudget)
        .filter(
            EnvelopeBudget.user_id == user_id,
            EnvelopeBudget.category_id == category_id,
            EnvelopeBudget.year == year,
            EnvelopeBudget.month == month,
        )
        .one_or_none()
    )
    return Decimal(row.budgeted_amount) if row is not None else Decimal("0")


def test_cover_overspend_rejects_non_positive_amount():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    travel = Category(user_id=user.id, name="Travel", type="expense")
    db.add_all([groceries, travel])
    db.commit()
    set_envelope_budget(db, user.id, groceries.id, 2026, 8, Decimal("100"))
    set_envelope_budget(db, user.id, travel.id, 2026, 8, Decimal("50"))

    with pytest.raises(ValueError):
        cover_overspend(db, user.id, travel.id, groceries.id, Decimal("0"), 2026, 8)

    assert _budgeted(db, user.id, groceries.id, 2026, 8) == Decimal("100")
    assert _budgeted(db, user.id, travel.id, 2026, 8) == Decimal("50")


def test_cover_overspend_rejects_same_category():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(groceries)
    db.commit()
    set_envelope_budget(db, user.id, groceries.id, 2026, 8, Decimal("100"))

    with pytest.raises(ValueError):
        cover_overspend(db, user.id, groceries.id, groceries.id, Decimal("20"), 2026, 8)

    assert _budgeted(db, user.id, groceries.id, 2026, 8) == Decimal("100")


def test_cover_overspend_rejects_insufficient_source_budget():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    travel = Category(user_id=user.id, name="Travel", type="expense")
    db.add_all([groceries, travel])
    db.commit()
    set_envelope_budget(db, user.id, groceries.id, 2026, 8, Decimal("0"))
    set_envelope_budget(db, user.id, travel.id, 2026, 8, Decimal("40"))

    with pytest.raises(ValueError):
        cover_overspend(db, user.id, travel.id, groceries.id, Decimal("100"), 2026, 8)

    assert _budgeted(db, user.id, groceries.id, 2026, 8) == Decimal("0")
    assert _budgeted(db, user.id, travel.id, 2026, 8) == Decimal("40")


def test_cover_endpoint_returns_400_on_insufficient_source_budget():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    travel = Category(user_id=user.id, name="Travel", type="expense")
    db.add_all([groceries, travel])
    db.commit()
    set_envelope_budget(db, user.id, groceries.id, 2026, 8, Decimal("0"))
    set_envelope_budget(db, user.id, travel.id, 2026, 8, Decimal("40"))

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.post(
            "/api/budget/envelopes/cover",
            json={
                "from_category_id": travel.id,
                "to_category_id": groceries.id,
                "amount": "100",
                "year": 2026,
                "month": 8,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["message"]
    finally:
        app.dependency_overrides.clear()
