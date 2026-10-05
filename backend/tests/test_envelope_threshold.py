from datetime import date
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import Category, Expense, Nudge, User
from app.foundation.envelope import check_envelope_threshold, set_envelope_budget


def _user_and_category(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()
    return user, category


def test_threshold_fires_bell_nudge_at_90_percent():
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))
    db.add(
        Expense(
            user_id=user.id, date=date(2026, 8, 5), amount=Decimal("95"),
            description="Big shop", category_id=category.id, source="manual",
        )
    )
    db.commit()

    nudge = check_envelope_threshold(db, user.id, category.id, 2026, 8)

    assert nudge is not None
    assert nudge.source == "budget_threshold"
    assert nudge.severity == "warning"
    assert db.query(Nudge).filter(Nudge.user_id == user.id).count() == 1


def test_threshold_does_not_duplicate_on_repeat_crossing():
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))
    db.add(
        Expense(
            user_id=user.id, date=date(2026, 8, 5), amount=Decimal("95"),
            description="Big shop", category_id=category.id, source="manual",
        )
    )
    db.commit()

    first = check_envelope_threshold(db, user.id, category.id, 2026, 8)
    second = check_envelope_threshold(db, user.id, category.id, 2026, 8)

    assert first is not None
    assert second is None
    assert db.query(Nudge).filter(Nudge.user_id == user.id).count() == 1


def test_threshold_escalates_to_critical_at_100_percent():
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))
    db.add(
        Expense(
            user_id=user.id, date=date(2026, 8, 5), amount=Decimal("95"),
            description="Big shop", category_id=category.id, source="manual",
        )
    )
    db.commit()
    check_envelope_threshold(db, user.id, category.id, 2026, 8)

    db.add(
        Expense(
            user_id=user.id, date=date(2026, 8, 10), amount=Decimal("10"),
            description="Top-up", category_id=category.id, source="manual",
        )
    )
    db.commit()
    second = check_envelope_threshold(db, user.id, category.id, 2026, 8)

    assert second is not None
    assert second.severity == "critical"
    assert db.query(Nudge).filter(Nudge.user_id == user.id).count() == 2


def test_threshold_returns_none_when_no_budget_set():
    db = _memory_db()
    user, category = _user_and_category(db)
    db.add(
        Expense(
            user_id=user.id, date=date(2026, 8, 5), amount=Decimal("95"),
            description="Big shop", category_id=category.id, source="manual",
        )
    )
    db.commit()

    assert check_envelope_threshold(db, user.id, category.id, 2026, 8) is None


from fastapi.testclient import TestClient

from app.main import app
from app.foundation.core.db import get_db
from app.foundation.auth import current_user


def test_create_expense_triggers_threshold_check():
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.post(
            "/api/budget/expenses",
            json={
                "date": "2026-08-05",
                "amount": "95",
                "currency": "EUR",
                "description": "Big shop",
                "category_id": category.id,
                "source": "manual",
            },
        )
        assert response.status_code == 200
        assert db.query(Nudge).filter(Nudge.user_id == user.id, Nudge.source == "budget_threshold").count() == 1
    finally:
        app.dependency_overrides.clear()


def test_create_expense_survives_threshold_check_failure(monkeypatch):
    """check_envelope_threshold is a best-effort side effect; if it raises, the
    expense must still be created and the endpoint must still return 200 —
    not a 500 that could prompt the client to retry and duplicate the expense."""
    db = _memory_db()
    user, category = _user_and_category(db)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))

    def _boom(*args, **kwargs):
        raise ValueError("simulated envelope threshold failure")

    monkeypatch.setattr("app.interface.api.budget.check_envelope_threshold", _boom)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.post(
            "/api/budget/expenses",
            json={
                "date": "2026-08-05",
                "amount": "95",
                "currency": "EUR",
                "description": "Big shop",
                "category_id": category.id,
                "source": "manual",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["description"] == "Big shop"
        assert db.query(Expense).filter(Expense.user_id == user.id).count() == 1
        # No nudge is created because check_envelope_threshold failed before
        # it could write one — the important assertion is that the expense
        # itself was persisted and the request did not 500.
        assert db.query(Nudge).filter(Nudge.user_id == user.id, Nudge.source == "budget_threshold").count() == 0
    finally:
        app.dependency_overrides.clear()
