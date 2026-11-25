from unittest.mock import MagicMock, patch

import httpx
from conftest import _memory_db

from app.foundation.settings import set_secret
from app.foundation.telegram_bot import get_bot_token, send_telegram_message


def test_get_bot_token_returns_none_when_unset():
    db = _memory_db()
    assert get_bot_token(db) is None


def test_get_bot_token_returns_stored_token():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")
    assert get_bot_token(db) == "123:ABC-token"


def test_send_telegram_message_posts_to_bot_api():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    with patch("httpx.Client.post", return_value=mock_response) as mock_post:
        result = send_telegram_message(db, "555", "hello")

    assert result is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.telegram.org/bot123:ABC-token/sendMessage"
    assert kwargs["json"] == {"chat_id": "555", "text": "hello"}


def test_send_telegram_message_returns_false_without_token():
    db = _memory_db()
    assert send_telegram_message(db, "555", "hello") is False


def test_send_telegram_message_returns_false_on_http_error():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")

    with patch("httpx.Client.post", side_effect=Exception("network error")):
        result = send_telegram_message(db, "555", "hello")

    assert result is False


def test_send_telegram_message_does_not_leak_token_on_http_error(caplog):
    """httpx.HTTPStatusError's default __str__ embeds the full request URL,
    which contains the bot token (Telegram authenticates via URL path, not
    headers). The logged error must never contain the token."""
    db = _memory_db()
    token = "123:SUPER-SECRET-TOKEN"
    set_secret(db, "telegram", token)

    request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/sendMessage")
    response = httpx.Response(status_code=401, request=request)
    error = httpx.HTTPStatusError("Unauthorized", request=request, response=response)

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(side_effect=error)
    with caplog.at_level("ERROR"):
        with patch("httpx.Client.post", return_value=mock_response):
            result = send_telegram_message(db, "555", "hello")

    assert result is False
    assert token not in caplog.text


def test_split_text_keeps_short_text_and_cuts_long_text_at_line_breaks():
    from app.foundation.telegram_bot import MAX_TEXT, split_text

    assert split_text("hello") == ["hello"]
    line = "x" * 99
    text = "\n".join([line] * 100)  # 9,999 characters
    parts = split_text(text)
    assert len(parts) == 3 and all(len(p) <= MAX_TEXT for p in parts)
    assert "\n".join(parts) == text
    assert [len(p) for p in split_text("y" * 5000)] == [4096, 904]  # no line break: a hard cut


def test_long_message_goes_out_in_parts_with_buttons_on_the_last():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")
    posted = []

    def fake_post(self, url, json=None):
        posted.append(json)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("httpx.Client.post", fake_post):
        ok = send_telegram_message(db, "555", "\n".join(["z" * 99] * 100), {"inline_keyboard": []})
    assert ok is True and len(posted) == 3
    assert "reply_markup" not in posted[0] and "reply_markup" in posted[2]


def test_rate_limit_is_retried_once_after_retry_after(monkeypatch):
    from app.foundation import telegram_bot

    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")
    waits = []
    monkeypatch.setattr(telegram_bot.time, "sleep", waits.append)
    answers = [429, 200]

    def fake_post(self, url, json=None):
        resp = MagicMock()
        resp.status_code = answers.pop(0)
        resp.json.return_value = {"ok": False, "parameters": {"retry_after": 3}}
        return resp

    with patch("httpx.Client.post", fake_post):
        assert send_telegram_message(db, "555", "hello") is True
    assert waits == [3.0] and answers == []


def test_rate_limit_longer_than_the_budget_is_not_slept_through(monkeypatch):
    from app.foundation import telegram_bot

    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")
    waits = []
    monkeypatch.setattr(telegram_bot.time, "sleep", waits.append)
    request = httpx.Request("POST", "https://api.telegram.org/bot123:ABC-token/sendMessage")

    def fake_post(self, url, json=None):
        resp = MagicMock()
        resp.status_code = 429
        resp.json.return_value = {"ok": False, "parameters": {"retry_after": 60}}
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(429, request=request))
        return resp

    with patch("httpx.Client.post", fake_post):
        assert send_telegram_message(db, "555", "hello") is False
    assert waits == []


def test_failed_later_part_still_counts_as_delivered_and_says_so():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC-token")
    posted = []

    def fake_post(self, url, json=None):
        posted.append(json)
        resp = MagicMock()
        resp.status_code = 200
        if len(posted) == 3:  # the third part fails
            resp.status_code = 500
            resp.raise_for_status.side_effect = RuntimeError("boom")
        return resp

    with patch("httpx.Client.post", fake_post):
        ok = send_telegram_message(db, "555", "\n".join(["z" * 99] * 100))
    assert ok is True  # the owner got parts 1 and 2: a caller must not resend them
    assert "could not be delivered" in posted[-1]["text"]


def test_error_description_is_logged_without_the_token(caplog):
    db = _memory_db()
    token = "123:SUPER-SECRET-TOKEN"
    set_secret(db, "telegram", token)
    request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/sendMessage")
    response = httpx.Response(400, request=request, json={"ok": False, "description": "Bad Request: chat not found"})
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("x", request=request, response=response))
    with caplog.at_level("ERROR"), patch("httpx.Client.post", return_value=mock_response):
        assert send_telegram_message(db, "555", "hello") is False
    assert "chat not found" in caplog.text and token not in caplog.text
