"""Telegram without a public webhook: getUpdates long polling run by the worker.

Every Telegram HTTP call is mocked at ``httpx.Client.post``; the DB is real.
The bot token is part of every Bot API URL, so no test may find it in a log.
"""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from conftest import _memory_db

from app.foundation import telegram_updates
from app.foundation.budget import ensure_default_categories
from app.foundation.expense_rules import seed_default_rules
from app.foundation.models.entities import Expense, User
from app.foundation.settings import get_public_settings, get_setting_row, set_secret, upsert_public_settings
from app.foundation.telegram_bot import generate_pairing_code, get_linked_account, verify_webhook_secret
from app.foundation.telegram_updates import (
    UPDATE_OFFSET_KEY,
    handle_update,
    poll_once,
    poll_telegram_updates,
    sync_webhook,
    telegram_mode,
)

TOKEN = "123456:SUPER-SECRET-BOT-TOKEN"


class FakeTelegram:
    """Stand-in for httpx.Client.post that answers by Bot API method name."""

    def __init__(self, updates=None, fail_get_updates=False):
        self.updates = list(updates or [])
        self.fail_get_updates = fail_get_updates
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, json=None, **kwargs):
        # Patched onto httpx.Client.post as an instance (not a function), so it is
        # not bound: there is no ``self``/client argument before ``url``.
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json or {}))
        request = httpx.Request("POST", url)
        if method == "getUpdates" and self.fail_get_updates:
            raise httpx.ConnectError(f"all connection attempts failed for {url}", request=request)
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": batch}, request=request)
        return httpx.Response(200, json={"ok": True, "result": True}, request=request)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


@pytest.fixture(autouse=True)
def _reset_process_state():
    telegram_updates._webhook_cleared_for = None
    yield
    telegram_updates._webhook_cleared_for = None


def _db_with_bot():
    db = _memory_db()
    set_secret(db, "telegram", TOKEN)
    return db


def _linked_user(db):
    from app.foundation.telegram_bot import link_pairing_code

    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    ensure_default_categories(db, user.id)
    seed_default_rules(db, user.id)
    link_pairing_code(db, generate_pairing_code(db, user.id).code, "777")
    return user


def _message(update_id, text, chat_id=777):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


# -- mode --------------------------------------------------------------------


def test_polling_is_the_default_mode():
    db = _memory_db()
    assert get_public_settings(db)["telegram_mode"] == "polling"
    assert telegram_mode(db) == "polling"


def test_unknown_stored_mode_degrades_to_polling():
    db = _memory_db()
    upsert_public_settings(db, {"telegram_mode": "carrier-pigeon"})
    assert telegram_mode(db) == "polling"


# -- polling -----------------------------------------------------------------


def test_poll_deletes_the_webhook_first_then_long_polls_with_a_timeout():
    db = _db_with_bot()
    fake = FakeTelegram()

    with patch("httpx.Client.post", fake):
        result = poll_once(db, long_poll_seconds=20)

    assert result == 0
    assert fake.methods() == ["deleteWebhook", "getUpdates"]
    delete_payload = fake.calls[0][1]
    poll_payload = fake.calls[1][1]
    assert delete_payload == {"drop_pending_updates": False}
    assert poll_payload["timeout"] == 20
    assert "offset" not in poll_payload
    assert poll_payload["allowed_updates"] == ["message"]


def test_webhook_is_only_deleted_once_per_token_and_process():
    db = _db_with_bot()
    fake = FakeTelegram()

    with patch("httpx.Client.post", fake):
        poll_once(db)
        poll_once(db)

    assert fake.methods() == ["deleteWebhook", "getUpdates", "getUpdates"]


def test_offset_is_persisted_and_sent_on_the_next_poll():
    db = _db_with_bot()
    user = _linked_user(db)
    fake = FakeTelegram(updates=[_message(100, "3.50 coffee"), _message(101, "12 REWE")])

    with patch("httpx.Client.post", fake), patch("app.foundation.telegram_bot.send_telegram_message", return_value=True):
        assert poll_once(db) == 2
        assert get_setting_row(db, UPDATE_OFFSET_KEY) == 102
        poll_once(db)

    assert [p.get("offset") for m, p in fake.calls if m == "getUpdates"] == [None, 102]
    assert {e.description for e in db.query(Expense).filter(Expense.user_id == user.id)} == {"coffee", "REWE"}


def test_offset_survives_a_restart():
    """The offset lives in app_settings, not in process memory."""
    db = _db_with_bot()
    upsert_public_settings(db, {UPDATE_OFFSET_KEY: 555})
    fake = FakeTelegram()

    with patch("httpx.Client.post", fake):
        poll_once(db)

    assert fake.calls[-1][1]["offset"] == 555


def test_offset_is_stored_before_the_handler_runs_so_a_crash_cannot_double_log():
    db = _db_with_bot()
    fake = FakeTelegram(updates=[_message(7, "1 a")])
    seen = {}

    def exploding_handler(_db, update):
        seen["offset_at_handling"] = get_setting_row(db, UPDATE_OFFSET_KEY)
        raise RuntimeError("boom")

    with patch("httpx.Client.post", fake), patch.object(telegram_updates, "handle_update", exploding_handler):
        assert poll_once(db) == 0  # the failure is contained

    assert seen["offset_at_handling"] == 8
    assert get_setting_row(db, UPDATE_OFFSET_KEY) == 8


def test_polling_uses_the_same_handler_as_the_webhook():
    db = _db_with_bot()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    code = generate_pairing_code(db, user.id).code
    fake = FakeTelegram(updates=[_message(1, f"/start {code}", chat_id=4242)])

    with patch("httpx.Client.post", fake), patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as sent:
        poll_once(db)

    assert get_linked_account(db, user.id).chat_id == "4242"
    assert sent.call_args[0][1] == "4242"


def test_updates_without_text_are_skipped_but_advance_the_offset():
    db = _db_with_bot()
    fake = FakeTelegram(updates=[{"update_id": 9, "edited_message": {}}, {"update_id": 10, "message": {"chat": {"id": 1}}}])

    with patch("httpx.Client.post", fake):
        assert poll_once(db) == 0

    assert get_setting_row(db, UPDATE_OFFSET_KEY) == 11


def test_no_poll_in_webhook_mode_or_without_a_token():
    fake = FakeTelegram()
    db = _db_with_bot()
    upsert_public_settings(db, {"telegram_mode": "webhook"})
    no_token = _memory_db()

    with patch("httpx.Client.post", fake):
        assert poll_once(db) is None
        assert poll_once(no_token) is None

    assert fake.calls == []


def test_a_failed_poll_clears_the_webhook_flag_and_never_logs_the_token(caplog):
    db = _db_with_bot()
    fake = FakeTelegram(fail_get_updates=True)

    with caplog.at_level(logging.DEBUG), patch("httpx.Client.post", fake):
        assert poll_once(db) is None
        assert telegram_updates._webhook_cleared_for is None
        assert poll_once(db) is None

    assert fake.methods().count("deleteWebhook") == 2  # re-cleared after the failure
    assert TOKEN not in caplog.text
    assert "SUPER-SECRET" not in caplog.text
    assert "ConnectError" in caplog.text


def test_http_error_status_is_logged_without_the_url(caplog):
    db = _db_with_bot()

    def conflict(client, url, json=None, **kwargs):
        request = httpx.Request("POST", url)
        status = 200 if url.endswith("deleteWebhook") else 409
        return httpx.Response(status, json={"ok": status == 200}, request=request)

    with caplog.at_level(logging.DEBUG), patch("httpx.Client.post", conflict):
        assert poll_once(db) is None

    assert "HTTP 409" in caplog.text
    assert TOKEN not in caplog.text


def test_successful_polling_logs_nothing_with_the_token(caplog):
    db = _db_with_bot()
    _linked_user(db)
    fake = FakeTelegram(updates=[_message(1, "1 a")])

    with caplog.at_level(logging.DEBUG), patch("httpx.Client.post", fake), patch(
        "app.foundation.telegram_bot.send_telegram_message", return_value=True
    ):
        poll_once(db)

    assert TOKEN not in caplog.text


def test_httpx_url_loggers_are_silenced_by_the_adapter():
    """httpx logs 'HTTP Request: POST https://api.telegram.org/bot<token>/...' at INFO."""
    db = _db_with_bot()
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)

    with patch("httpx.Client.post", FakeTelegram()):
        poll_once(db)

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_poll_loop_runs_until_a_poll_is_skipped(monkeypatch):
    results = iter([1, 2, None, 5])
    monkeypatch.setattr(telegram_updates, "poll_once", lambda db, long_poll_seconds: next(results))
    monkeypatch.setattr(telegram_updates.time, "sleep", lambda s: None)

    out = poll_telegram_updates(SimpleNamespace(), budget_seconds=50)

    assert out == {"polls": 2, "handled": 3}


def test_poll_loop_with_a_tiny_budget_does_not_poll(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_updates, "poll_once", lambda db, long_poll_seconds: calls.append(1) or 0)

    assert poll_telegram_updates(SimpleNamespace(), budget_seconds=2) == {"polls": 0, "handled": 0}
    assert calls == []


# -- webhook mode ------------------------------------------------------------


def test_sync_webhook_creates_the_secret_and_registers_the_url():
    db = _db_with_bot()
    upsert_public_settings(db, {"frontend_origin": "http://10.0.0.23,https://qf.example.ts.net"})
    fake = FakeTelegram()

    with patch("httpx.Client.post", fake):
        assert sync_webhook(db) is True

    method, payload = fake.calls[-1]
    assert method == "setWebhook"
    assert payload["url"] == "https://qf.example.ts.net/api/telegram/webhook"
    assert payload["allowed_updates"] == ["message"]
    # The secret Telegram will echo back is the one the endpoint verifies against.
    assert verify_webhook_secret(db, payload["secret_token"]) is True


def test_sync_webhook_without_an_https_origin_still_prepares_the_secret(caplog):
    db = _db_with_bot()
    fake = FakeTelegram()

    with caplog.at_level(logging.WARNING), patch("httpx.Client.post", fake):
        assert sync_webhook(db) is False

    assert fake.calls == []
    from app.foundation.settings import get_secret

    assert get_secret(db, "telegram")[1].get("webhook_secret")
    assert "https" in caplog.text


def test_sync_webhook_without_a_bot_token_does_nothing():
    fake = FakeTelegram()
    with patch("httpx.Client.post", fake):
        assert sync_webhook(_memory_db()) is False
    assert fake.calls == []


def test_switching_to_webhook_mode_in_settings_makes_the_endpoint_usable():
    """ensure_webhook_secret used to be called by nobody: the webhook failed closed (403) forever."""
    from app.foundation.schemas import SettingsPayload
    from app.foundation.settings import get_secret
    from app.interface.api.settings import update_settings

    db = _db_with_bot()
    assert get_secret(db, "telegram")[1].get("webhook_secret") is None

    with patch("httpx.Client.post", FakeTelegram()):
        update_settings(SettingsPayload(settings={"telegram_mode": "webhook"}), db, None)

    secret = get_secret(db, "telegram")[1]["webhook_secret"]
    assert verify_webhook_secret(db, secret) is True


def test_saving_other_settings_in_polling_mode_touches_no_telegram_secret():
    from app.foundation.schemas import SettingsPayload
    from app.foundation.settings import get_secret
    from app.interface.api.settings import update_settings

    db = _db_with_bot()
    fake = FakeTelegram()
    with patch("httpx.Client.post", fake):
        update_settings(SettingsPayload(settings={"monthly_contribution_eur": 750}), db, None)

    assert fake.calls == []
    assert get_secret(db, "telegram")[1] == {}


# -- shared handler ----------------------------------------------------------


def test_handle_update_ignores_non_message_shapes():
    db = _memory_db()
    assert handle_update(db, {}) is False
    assert handle_update(db, {"message": {"chat": {}, "text": "hi"}}) is False
    assert handle_update(db, {"message": {"chat": {"id": 1}, "text": "   "}}) is False
