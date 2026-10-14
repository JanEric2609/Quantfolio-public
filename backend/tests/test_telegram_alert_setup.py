"""Telegram is the channel for the self-check and crash-guard alerts: the
Control Center says when it is missing or unlinked, and the owner can send a
test message to the linked chat."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.control_center import attention_items
from app.foundation.core.db import Base
from app.foundation.models.entities import TelegramAccount, User
from app.foundation.settings import set_secret


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _admin(db, role="admin"):
    user = User(username=f"owner-{role}", password_hash="x", role=role)
    db.add(user)
    db.commit()
    return user


def _telegram_ids(db):
    return {item["id"] for item in attention_items(db) if item["id"].startswith("telegram-")}


def test_no_bot_token_is_a_warning():
    db = _memory_db()
    _admin(db)
    [item] = [i for i in attention_items(db) if i["id"] == "telegram-missing"]
    assert item["severity"] == "warning"
    assert item["href"].endswith("?connection=telegram")


def test_a_token_without_an_admin_chat_is_a_warning():
    db = _memory_db()
    admin = _admin(db)
    member = _admin(db, role="user")
    set_secret(db, "telegram", "123:abc")
    db.add(TelegramAccount(user_id=member.id, chat_id="7"))  # a non-admin's chat gets no alerts
    db.commit()
    assert _telegram_ids(db) == {"telegram-unlinked"}

    db.add(TelegramAccount(user_id=admin.id, chat_id="42"))
    db.commit()
    assert _telegram_ids(db) == set()


def _client(db, user):
    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return app, TestClient(app)


def test_test_message_goes_to_the_linked_chat(monkeypatch):
    db = _memory_db()
    user = _admin(db)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("app.interface.api.telegram.send_telegram_message",
                        lambda db, chat_id, text: sent.append((chat_id, text)) or True)
    app, client = _client(db, user)
    try:
        assert client.post("/api/telegram/test-message").json() == {"sent": False, "message": "No bot token saved."}

        set_secret(db, "telegram", "123:abc")
        body = client.post("/api/telegram/test-message").json()
        assert body["sent"] is False and "pairing code" in body["message"]

        db.add(TelegramAccount(user_id=user.id, chat_id="42"))
        db.commit()
        assert client.post("/api/telegram/test-message").json()["sent"] is True
        assert sent and sent[0][0] == "42"
    finally:
        app.dependency_overrides.clear()
