from datetime import UTC, datetime, timedelta, timezone

from conftest import _memory_db

from app.foundation.models.entities import TelegramAccount, TelegramPairingCode, User
from app.foundation.settings import get_secret, set_secret
from app.foundation.telegram_bot import (
    ensure_webhook_secret,
    generate_pairing_code,
    get_account_by_chat_id,
    get_linked_account,
    link_pairing_code,
    verify_webhook_secret,
)


def _make_user(db, username="jan"):
    user = User(username=username, password_hash="hash")
    db.add(user)
    db.commit()
    return user


def test_generate_pairing_code_creates_row_with_future_expiry():
    db = _memory_db()
    user = _make_user(db)
    pairing = generate_pairing_code(db, user.id)
    assert pairing.user_id == user.id
    assert len(pairing.code) >= 6
    expires_at = pairing.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > datetime.now(timezone.utc)


def test_generate_pairing_code_replaces_prior_pending_code():
    db = _memory_db()
    user = _make_user(db)
    first = generate_pairing_code(db, user.id)
    second = generate_pairing_code(db, user.id)
    assert first.code != second.code
    remaining = db.query(TelegramPairingCode).filter(TelegramPairingCode.user_id == user.id).all()
    assert len(remaining) == 1
    assert remaining[0].code == second.code


def test_link_pairing_code_success_creates_account():
    db = _memory_db()
    user = _make_user(db)
    pairing = generate_pairing_code(db, user.id)

    account = link_pairing_code(db, pairing.code, "555999")

    assert account is not None
    assert account.user_id == user.id
    assert account.chat_id == "555999"
    assert db.query(TelegramPairingCode).filter(TelegramPairingCode.user_id == user.id).first() is None


def test_link_pairing_code_unknown_code_returns_none():
    db = _memory_db()
    assert link_pairing_code(db, "NOPE99", "555999") is None


def test_link_pairing_code_expired_returns_none():
    db = _memory_db()
    user = _make_user(db)
    expired = TelegramPairingCode(
        user_id=user.id, code="OLD123", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.add(expired)
    db.commit()

    assert link_pairing_code(db, "OLD123", "555999") is None


def test_link_pairing_code_relinking_replaces_chat_id():
    db = _memory_db()
    user = _make_user(db)
    first_pairing = generate_pairing_code(db, user.id)
    link_pairing_code(db, first_pairing.code, "111")

    second_pairing = generate_pairing_code(db, user.id)
    account = link_pairing_code(db, second_pairing.code, "222")

    assert account.chat_id == "222"
    assert db.query(TelegramAccount).filter(TelegramAccount.user_id == user.id).count() == 1


def test_get_linked_account_and_get_account_by_chat_id():
    db = _memory_db()
    user = _make_user(db)
    pairing = generate_pairing_code(db, user.id)
    link_pairing_code(db, pairing.code, "555999")

    assert get_linked_account(db, user.id).chat_id == "555999"
    assert get_account_by_chat_id(db, "555999").user_id == user.id
    assert get_account_by_chat_id(db, "unknown") is None


def test_ensure_webhook_secret_is_stable_across_calls():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")
    first = ensure_webhook_secret(db)
    second = ensure_webhook_secret(db)
    assert first == second
    assert len(first) >= 16


def test_ensure_webhook_secret_without_bot_token_is_noop():
    # Setting up the webhook secret before a bot token has been saved must not
    # create a phantom "telegram" ApiKey row (that would make
    # integration_presence() falsely report Telegram as configured).
    db = _memory_db()
    token = ensure_webhook_secret(db)
    assert len(token) >= 16
    assert get_secret(db, "telegram") is None


def test_verify_webhook_secret_matches_and_rejects():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")
    secret = ensure_webhook_secret(db)
    assert verify_webhook_secret(db, secret) is True
    assert verify_webhook_secret(db, "wrong") is False
    assert verify_webhook_secret(db, None) is False
