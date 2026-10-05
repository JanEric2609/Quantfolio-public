from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import Category, Expense, User
from app.foundation.budget import ensure_default_categories
from app.foundation.expense_rules import seed_default_rules
from app.foundation.telegram_bot import create_expense_from_telegram, parse_freeform_expense


def test_parse_freeform_expense_dot_decimal():
    assert parse_freeform_expense("12.50 groceries") == (Decimal("12.50"), "groceries")


def test_parse_freeform_expense_comma_decimal():
    assert parse_freeform_expense("12,50 REWE") == (Decimal("12.50"), "REWE")


def test_parse_freeform_expense_integer_amount():
    assert parse_freeform_expense("5 coffee") == (Decimal("5"), "coffee")


def test_parse_freeform_expense_no_leading_number_returns_none():
    assert parse_freeform_expense("groceries 12.50") is None
    assert parse_freeform_expense("hello") is None


def test_parse_freeform_expense_strips_whitespace():
    assert parse_freeform_expense("  3.50   coffee run  ") == (Decimal("3.50"), "coffee run")


def _setup_user_with_rules(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    ensure_default_categories(db, user.id)
    seed_default_rules(db, user.id)
    return user


def test_create_expense_from_telegram_categorizes_via_rules():
    db = _memory_db()
    user = _setup_user_with_rules(db)

    expense = create_expense_from_telegram(db, user.id, "12.50 REWE")

    assert expense is not None
    assert expense.amount == Decimal("12.50")
    assert expense.description == "REWE"
    assert expense.source == "telegram"
    category = db.get(Category, expense.category_id)
    assert category.name == "Groceries"


def test_create_expense_from_telegram_leaves_uncategorized_when_no_rule_matches():
    db = _memory_db()
    user = _setup_user_with_rules(db)

    expense = create_expense_from_telegram(db, user.id, "9.99 some random shop")

    assert expense is not None
    assert expense.category_id is None


def test_create_expense_from_telegram_returns_none_for_unparseable_text():
    db = _memory_db()
    user = _setup_user_with_rules(db)

    assert create_expense_from_telegram(db, user.id, "not an expense") is None
    assert db.query(Expense).filter(Expense.user_id == user.id).count() == 0
