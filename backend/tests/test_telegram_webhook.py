from unittest.mock import patch

from fastapi.testclient import TestClient

from conftest import _memory_db

from app.main import app
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation.budget import ensure_default_categories
from app.foundation.expense_rules import seed_default_rules
from app.foundation.telegram_bot import ensure_webhook_secret, generate_pairing_code


def _client_with_db(db):
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    return client


def _teardown():
    app.dependency_overrides.clear()


def _make_linked_setup(db):
    from app.foundation.settings import set_secret

    set_secret(db, "telegram", "123:ABC")
    secret = ensure_webhook_secret(db)
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    ensure_default_categories(db, user.id)
    seed_default_rules(db, user.id)
    return user, secret


def test_webhook_rejects_missing_secret_header():
    db = _memory_db()
    client = _client_with_db(db)
    try:
        response = client.post("/api/telegram/webhook", json={"message": {"chat": {"id": 1}, "text": "hi"}})
        assert response.status_code == 403
    finally:
        _teardown()


def test_webhook_links_account_via_start_command():
    db = _memory_db()
    user, secret = _make_linked_setup(db)
    pairing = generate_pairing_code(db, user.id)
    client = _client_with_db(db)
    try:
        with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
            response = client.post(
                "/api/telegram/webhook",
                json={"message": {"chat": {"id": 777}, "text": f"/start {pairing.code}"}},
                headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            )
        assert response.status_code == 200
        assert mock_send.called
        from app.foundation.telegram_bot import get_linked_account

        assert get_linked_account(db, user.id).chat_id == "777"
    finally:
        _teardown()


def test_webhook_logs_freeform_expense_from_linked_chat():
    db = _memory_db()
    user, secret = _make_linked_setup(db)
    from app.foundation.telegram_bot import link_pairing_code

    pairing = generate_pairing_code(db, user.id)
    link_pairing_code(db, pairing.code, "777")

    client = _client_with_db(db)
    try:
        with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True):
            response = client.post(
                "/api/telegram/webhook",
                json={"message": {"chat": {"id": 777}, "text": "12.50 REWE"}},
                headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            )
        assert response.status_code == 200
        from app.foundation.models.entities import Expense

        expense = db.query(Expense).filter(Expense.user_id == user.id).one()
        assert expense.description == "REWE"
    finally:
        _teardown()


def test_webhook_from_unlinked_chat_is_ignored_gracefully():
    db = _memory_db()
    _user, secret = _make_linked_setup(db)
    client = _client_with_db(db)
    try:
        with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
            response = client.post(
                "/api/telegram/webhook",
                json={"message": {"chat": {"id": 999}, "text": "12.50 REWE"}},
                headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            )
        assert response.status_code == 200
        sent_text = mock_send.call_args[0][2]
        assert "not linked" in sent_text.lower() or "link" in sent_text.lower()
    finally:
        _teardown()
