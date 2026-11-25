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
    user = User(username="owner", password_hash="hash")
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


def _linked(db):
    from app.foundation.models.entities import TelegramAccount, User

    user = User(username="owner", password_hash="x", role="admin")
    db.add(user)
    db.flush()
    db.add(TelegramAccount(user_id=user.id, chat_id="42"))
    db.commit()


def test_large_amount_is_not_logged_without_a_bang(monkeypatch):
    """A fill price typed without Reply ("24003.5 0.04") must not become a 24,003.50 EUR expense."""
    from conftest import _memory_db

    from app.foundation import telegram_bot, telegram_updates
    from app.foundation.models.entities import Expense

    db = _memory_db()
    _linked(db)
    sent = []
    monkeypatch.setattr(telegram_bot, "send_telegram_message", lambda db_, chat, text, markup=None: sent.append(text))
    telegram_updates.handle_message(db, "42", "24003.5 0.04")
    assert db.query(Expense).count() == 0 and "Not logged" in sent[-1]
    telegram_updates.handle_message(db, "42", "1500 laptop !")
    assert db.query(Expense).count() == 1 and db.query(Expense).one().description == "laptop"
    telegram_updates.handle_message(db, "42", "3.50 coffee")
    assert db.query(Expense).count() == 2


def test_a_bang_on_a_small_expense_is_kept_in_the_description(monkeypatch):
    from conftest import _memory_db

    from app.foundation import telegram_bot, telegram_updates
    from app.foundation.models.entities import Expense

    db = _memory_db()
    _linked(db)
    monkeypatch.setattr(telegram_bot, "send_telegram_message", lambda db_, chat, text, markup=None: None)
    telegram_updates.handle_message(db, "42", "12.50 lunch!")
    assert db.query(Expense).one().description == "lunch!"
