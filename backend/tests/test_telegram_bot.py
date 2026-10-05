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
