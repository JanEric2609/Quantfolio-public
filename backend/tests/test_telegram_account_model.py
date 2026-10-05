from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from conftest import _memory_db

from app.foundation.models.entities import TelegramAccount, TelegramPairingCode, User


def test_telegram_account_unique_user_and_chat_id():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    db.add(TelegramAccount(user_id=user.id, chat_id="12345"))
    db.commit()

    other = User(username="alex", password_hash="hash")
    db.add(other)
    db.commit()

    db.add(TelegramAccount(user_id=other.id, chat_id="12345"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_telegram_pairing_code_unique_code():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)

    db.add(TelegramPairingCode(user_id=user.id, code="ABC123", expires_at=expires))
    db.commit()

    other = User(username="alex", password_hash="hash")
    db.add(other)
    db.commit()

    db.add(TelegramPairingCode(user_id=other.id, code="ABC123", expires_at=expires))
    with pytest.raises(IntegrityError):
        db.commit()
