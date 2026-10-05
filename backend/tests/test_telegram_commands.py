from datetime import date
from decimal import Decimal

from conftest import _memory_db

from app.foundation.models.entities import Category, Expense, User
from app.foundation.envelope import set_envelope_budget
from app.foundation.telegram_replies import format_balance_reply, format_summary_reply


def _setup_user(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    groceries = Category(user_id=user.id, name="Groceries", type="expense")
    transport = Category(user_id=user.id, name="Transport", type="expense")
    db.add_all([groceries, transport])
    db.commit()
    return user, groceries, transport


def test_format_balance_reply_lists_envelopes():
    db = _memory_db()
    user, groceries, transport = _setup_user(db)
    today = date.today()
    set_envelope_budget(db, user.id, groceries.id, today.year, today.month, Decimal("200"))
    db.add(Expense(user_id=user.id, date=today, amount=Decimal("50"), description="REWE", category_id=groceries.id))
    db.commit()

    reply = format_balance_reply(db, user.id)

    assert "Groceries" in reply
    assert "150" in reply or "150.0" in reply  # remaining
    assert "Transport" in reply


def test_format_balance_reply_no_categories():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    reply = format_balance_reply(db, user.id)

    assert isinstance(reply, str)
    assert len(reply) > 0


def test_format_summary_reply_shows_spent_and_income():
    db = _memory_db()
    user, groceries, _transport = _setup_user(db)
    today = date.today()
    db.add(Expense(user_id=user.id, date=today, amount=Decimal("50"), description="REWE", category_id=groceries.id))
    db.commit()

    reply = format_summary_reply(db, user.id)

    assert "50" in reply
