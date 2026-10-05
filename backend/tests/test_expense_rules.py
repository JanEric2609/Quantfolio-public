from datetime import date
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import Expense, ExpenseRule, User
from app.foundation.budget import ensure_default_categories
from app.foundation.expense_rules import (
    apply_rules,
    llm_categorize_uncategorized,
    seed_default_rules,
)


def test_seed_default_rules_creates_system_rules_idempotently():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    rules = seed_default_rules(db, user.id)
    assert len(rules) > 0
    system_count = len([r for r in rules if r.source == "system"])
    assert system_count > 0

    rules_again = seed_default_rules(db, user.id)
    assert len(rules_again) == len(rules)


def test_apply_rules_respects_priority_order():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    categories = ensure_default_categories(db, user.id)
    cat_by_name = {c.name: c for c in categories}

    groceries = cat_by_name["Groceries"]
    transport = cat_by_name["Transport"]

    db.add_all(
        [
            ExpenseRule(user_id=user.id, pattern="REWE", match_type="contains", category_id=groceries.id, priority=10, source="user"),
            ExpenseRule(user_id=user.id, pattern="REWE", match_type="contains", category_id=transport.id, priority=5, source="user"),
        ]
    )
    db.commit()

    db.add(Expense(user_id=user.id, date=date(2026, 5, 1), amount=Decimal("-12.50"), description="REWE Markt"))
    db.commit()

    count = apply_rules(db, user.id)
    assert count == 1

    expense = db.query(Expense).filter(Expense.user_id == user.id).one()
    assert expense.category_id == transport.id


def test_apply_rules_skips_already_categorized():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    categories = ensure_default_categories(db, user.id)
    groceries = [c for c in categories if c.name == "Groceries"][0]

    db.add(Expense(user_id=user.id, date=date(2026, 5, 1), amount=Decimal("-10"), description="REWE", category_id=groceries.id))
    db.commit()

    db.add(ExpenseRule(user_id=user.id, pattern="REWE", match_type="contains", category_id=groceries.id, priority=10, source="user"))
    db.commit()

    count = apply_rules(db, user.id)
    assert count == 0


def test_llm_categorize_uncategorized_with_mock_llm():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    categories = ensure_default_categories(db, user.id)
    cat_by_name = {c.name: c for c in categories}

    db.add_all(
        [
            Expense(user_id=user.id, date=date(2026, 5, 1), amount=Decimal("-50"), description="Unknown shop"),
            Expense(user_id=user.id, date=date(2026, 5, 2), amount=Decimal("-20"), description="Another place"),
        ]
    )
    db.commit()

    def mock_llm(prompt: str) -> str:
        return '{"1": "Groceries", "2": "Transport"}'

    count = llm_categorize_uncategorized(db, user.id, mock_llm, batch_size=10)
    assert count == 2

    expenses = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.date.asc()).all()
    assert expenses[0].category_id == cat_by_name["Groceries"].id
    assert expenses[1].category_id == cat_by_name["Transport"].id


def test_llm_categorize_uncategorized_bad_json_skips():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    ensure_default_categories(db, user.id)

    db.add(Expense(user_id=user.id, date=date(2026, 5, 1), amount=Decimal("-50"), description="Unknown shop"))
    db.commit()

    def bad_llm(prompt: str) -> str:
        return "not json at all"

    count = llm_categorize_uncategorized(db, user.id, bad_llm, batch_size=10)
    assert count == 0

    expense = db.query(Expense).filter(Expense.user_id == user.id).one()
    assert expense.category_id is None


def test_apply_rules_regex_match():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    categories = ensure_default_categories(db, user.id)
    transport = [c for c in categories if c.name == "Transport"][0]

    db.add(
        ExpenseRule(
            user_id=user.id,
            pattern=r"\bDB\b",
            match_type="regex",
            category_id=transport.id,
            priority=10,
            source="user",
        )
    )
    db.commit()

    db.add(Expense(user_id=user.id, date=date(2026, 5, 1), amount=Decimal("-29"), description="DB Bahn Ticket"))
    db.commit()

    count = apply_rules(db, user.id)
    assert count == 1

    expense = db.query(Expense).filter(Expense.user_id == user.id).one()
    assert expense.category_id == transport.id


def test_apply_rules_invalid_regex_logged_and_skipped():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    categories = ensure_default_categories(db, user.id)
    transport = [c for c in categories if c.name == "Transport"][0]

    db.add(
        ExpenseRule(
            user_id=user.id,
            pattern=r"[invalid(",
            match_type="regex",
            category_id=transport.id,
            priority=10,
            source="user",
        )
    )
    db.commit()

    db.add(Expense(user_id=user.id, date=date(2026, 5, 1), amount=Decimal("-29"), description="DB Bahn"))
    db.commit()

    count = apply_rules(db, user.id)
    assert count == 0
