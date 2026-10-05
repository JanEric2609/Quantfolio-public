from datetime import date
from decimal import Decimal

from conftest import _memory_db
from fastapi.testclient import TestClient

from app.foundation.core.db import get_db
from app.main import app
from app.foundation.models.entities import Category, Expense, User
from app.foundation.auth import current_user
from app.foundation.budget_insights import monthly_review_summary, potential_duplicate_pairs
from app.foundation.envelope import set_envelope_budget


def test_potential_duplicate_pairs_matches_manual_and_dkb_within_tolerance():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.add_all(
        [
            Expense(
                user_id=user.id,
                date=date(2026, 8, 3),
                amount=Decimal("-12.50"),
                description="Coffee shop",
                source="manual",
            ),
            Expense(
                user_id=user.id,
                date=date(2026, 8, 4),
                amount=Decimal("-12.50"),
                description="KARTENZAHLUNG COFFEE SHOP",
                source="dkb_auto",
            ),
        ]
    )
    db.commit()

    pairs = potential_duplicate_pairs(db, user.id, 2026, 8)

    assert len(pairs) == 1
    assert pairs[0]["amount"] == 12.50


def test_potential_duplicate_pairs_ignores_amounts_outside_tolerance():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.add_all(
        [
            Expense(user_id=user.id, date=date(2026, 8, 3), amount=Decimal("-12.50"), description="Coffee", source="manual"),
            Expense(user_id=user.id, date=date(2026, 8, 3), amount=Decimal("-20.00"), description="Groceries", source="dkb_auto"),
        ]
    )
    db.commit()

    assert potential_duplicate_pairs(db, user.id, 2026, 8) == []


def test_potential_duplicate_pairs_ignores_dates_outside_tolerance():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.add_all(
        [
            Expense(user_id=user.id, date=date(2026, 8, 1), amount=Decimal("-12.50"), description="Coffee", source="manual"),
            Expense(user_id=user.id, date=date(2026, 8, 20), amount=Decimal("-12.50"), description="Coffee", source="dkb_auto"),
        ]
    )
    db.commit()

    assert potential_duplicate_pairs(db, user.id, 2026, 8) == []


def test_monthly_review_summary_includes_uncategorized_duplicates_and_sinking_funds():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    sinking_fund = Category(
        user_id=user.id, name="Laptop Fund", type="expense", target_amount=Decimal("1200"), target_date=date(2027, 1, 1)
    )
    eating_out = Category(user_id=user.id, name="Eating Out", type="expense")
    db.add_all([sinking_fund, eating_out])
    db.commit()
    set_envelope_budget(db, user.id, sinking_fund.id, 2026, 8, Decimal("100"))
    db.add_all(
        [
            Expense(user_id=user.id, date=date(2026, 8, 5), amount=Decimal("-15"), description="Mystery charge", source="manual"),
            Expense(
                user_id=user.id,
                date=date(2026, 8, 3),
                amount=Decimal("-12.50"),
                description="Coffee",
                category_id=eating_out.id,
                source="manual",
            ),
            Expense(
                user_id=user.id,
                date=date(2026, 8, 4),
                amount=Decimal("-12.50"),
                description="Coffee",
                category_id=eating_out.id,
                source="dkb_auto",
            ),
        ]
    )
    db.commit()

    summary = monthly_review_summary(db, user.id, 2026, 8)

    assert summary["year"] == 2026
    assert summary["month"] == 8
    assert len(summary["uncategorized_expenses"]) == 1
    assert summary["uncategorized_expenses"][0]["description"] == "Mystery charge"
    assert len(summary["potential_duplicates"]) == 1
    assert len(summary["sinking_funds"]) == 1
    assert summary["sinking_funds"][0]["category_name"] == "Laptop Fund"
    assert summary["sinking_funds"][0]["progress"] == 100.0


def test_monthly_review_endpoint_round_trip():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    client = TestClient(app)
    try:
        response = client.get("/api/budget/review", params={"year": 2026, "month": 8})
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "year": 2026,
            "month": 8,
            "uncategorized_expenses": [],
            "potential_duplicates": [],
            "sinking_funds": [],
        }
    finally:
        app.dependency_overrides.clear()
