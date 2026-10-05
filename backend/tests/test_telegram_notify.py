from unittest.mock import patch

from conftest import _memory_db

from app.foundation.models.entities import Nudge, User
from app.foundation.telegram_bot import link_pairing_code
from app.foundation.notify import send_notification


def _linked_user(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    from app.foundation.telegram_bot import generate_pairing_code

    pairing = generate_pairing_code(db, user.id)
    link_pairing_code(db, pairing.code, "42")
    return user


def test_send_notification_telegram_delivers_when_linked():
    db = _memory_db()
    user = _linked_user(db)
    nudge = Nudge(user_id=user.id, source="budget_threshold", severity="warning", payload_json="{}")
    db.add(nudge)
    db.commit()

    with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
        result = send_notification(db, user.id, "telegram", nudge)

    assert result is True
    assert mock_send.call_args[0][1] == "42"


def test_send_notification_telegram_no_op_when_unlinked():
    db = _memory_db()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    nudge = Nudge(user_id=user.id, source="budget_threshold", severity="warning", payload_json="{}")
    db.add(nudge)
    db.commit()

    result = send_notification(db, user.id, "telegram", nudge)

    assert result is False
