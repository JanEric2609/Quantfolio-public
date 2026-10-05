from datetime import date
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import Category, Expense, User
from app.foundation.envelope import set_envelope_budget, sinking_fund_progress


def _user_and_goal_category(db, target_amount="1200", target_year=2027, target_month=1):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(
        user_id=user.id,
        name="Laptop Fund",
        type="expense",
        target_amount=Decimal(target_amount),
        target_date=date(target_year, target_month, 1),
    )
    db.add(category)
    db.commit()
    return user, category


def test_sinking_fund_progress_returns_none_when_no_target_set():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()

    assert sinking_fund_progress(db, user.id, category, 2026, 8) is None


def test_sinking_fund_progress_mid_goal_computes_required_monthly():
    db = _memory_db()
    user, category = _user_and_goal_category(db, target_amount="1200", target_year=2027, target_month=1)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("200"))

    result = sinking_fund_progress(db, user.id, category, 2026, 8)

    assert result["target_amount"] == 1200.0
    assert result["target_date"] == "2027-01-01"
    assert result["progress"] == 200.0
    # 1000 remaining / 6 months remaining (Aug..Jan inclusive) = 166.67
    assert round(result["required_monthly"], 2) == 166.67


def test_sinking_fund_progress_floors_months_remaining_at_one_when_overdue():
    db = _memory_db()
    user, category = _user_and_goal_category(db, target_amount="500", target_year=2026, target_month=1)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("100"))

    result = sinking_fund_progress(db, user.id, category, 2026, 8)

    assert result["progress"] == 100.0
    assert result["required_monthly"] == 400.0


def test_sinking_fund_progress_zero_when_goal_reached():
    db = _memory_db()
    user, category = _user_and_goal_category(db, target_amount="500", target_year=2027, target_month=1)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("600"))

    result = sinking_fund_progress(db, user.id, category, 2026, 8)

    assert result["progress"] == 600.0
    assert result["required_monthly"] == 0.0


def test_sinking_fund_progress_reduces_after_withdrawal_expense():
    db = _memory_db()
    user, category = _user_and_goal_category(db, target_amount="1200", target_year=2027, target_month=1)
    set_envelope_budget(db, user.id, category.id, 2026, 8, Decimal("1200"))
    db.add(
        Expense(
            user_id=user.id,
            date=date(2026, 8, 15),
            amount=Decimal("1200"),
            description="Bought the laptop",
            category_id=category.id,
            source="manual",
        )
    )
    db.commit()

    result = sinking_fund_progress(db, user.id, category, 2026, 8)

    assert result["progress"] == 0.0
