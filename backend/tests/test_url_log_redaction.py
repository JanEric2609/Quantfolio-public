import logging

import httpx

from app.foundation.core.logging_setup import quiet_url_loggers as _quiet_url_loggers


def test_httpx_request_urls_with_api_keys_are_not_logged(caplog):
    # httpx logs "HTTP Request: GET <full url>" at INFO; provider API keys ride
    # in the query string and the Telegram token in the path.
    _quiet_url_loggers()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with caplog.at_level(logging.INFO), httpx.Client(transport=transport) as client:
        client.get("https://api.example.test/bot123:SECRETTOKEN/getMe?apikey=SECRETKEY")
    assert "SECRET" not in caplog.text
