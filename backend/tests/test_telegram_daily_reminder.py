from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from conftest import _memory_db

from app.foundation.models.entities import Category, Expense, User
from app.foundation.telegram_bot import generate_pairing_code, link_pairing_code
from app.worker import send_telegram_daily_reminders


def _linked_user(db, username="jan"):
    user = User(username=username, password_hash="hash")
    db.add(user)
    db.commit()
    pairing = generate_pairing_code(db, user.id)
    link_pairing_code(db, pairing.code, f"chat-{username}")
    return user


def test_daily_reminder_sent_when_nothing_logged_today():
    db = _memory_db()
    _linked_user(db)

    with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
        result = send_telegram_daily_reminders(db)

    assert result["reminded"] == 1
    assert mock_send.called


def test_daily_reminder_skipped_when_already_logged_today():
    db = _memory_db()
    user = _linked_user(db)
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()
    db.add(Expense(user_id=user.id, date=date.today(), amount=Decimal("5"), description="coffee", category_id=category.id))
    db.commit()

    with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
        result = send_telegram_daily_reminders(db)

    assert result["reminded"] == 0
    assert not mock_send.called


def test_daily_reminder_ignores_expenses_from_other_days():
    db = _memory_db()
    user = _linked_user(db)
    category = Category(user_id=user.id, name="Groceries", type="expense")
    db.add(category)
    db.commit()
    db.add(
        Expense(
            user_id=user.id,
            date=date.today() - timedelta(days=1),
            amount=Decimal("5"),
            description="coffee",
            category_id=category.id,
        )
    )
    db.commit()

    with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True):
        result = send_telegram_daily_reminders(db)

    assert result["reminded"] == 1


def test_daily_reminder_skips_unlinked_users():
    db = _memory_db()
    user = User(username="unlinked", password_hash="hash")
    db.add(user)
    db.commit()

    with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
        result = send_telegram_daily_reminders(db)

    assert result["reminded"] == 0
    assert not mock_send.called
