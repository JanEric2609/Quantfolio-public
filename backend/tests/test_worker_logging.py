"""The worker configures logging like the API does (audit D2).

Before, ``python -m app.worker`` never configured logging, so the root logger
stayed at WARNING and every INFO job log (``price_backfill_daily: ...``) was
dropped. The URL-logging libraries must also be pinned to WARNING there: their
request lines carry provider API keys and the Telegram bot token.
"""
from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace

import httpx

from app import worker
from app.foundation.core import config as core_config
from app.foundation.core.logging_setup import LOG_FORMAT, URL_LOGGERS, configure_logging


@contextlib.contextmanager
def _unconfigured_root():
    """An unconfigured root logger, as in a fresh worker process (restored after).

    pytest attaches its capture handlers to the root logger for each test phase,
    which would make ``logging.basicConfig`` a no-op, so they are lifted here.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_url_levels = {name: logging.getLogger(name).level for name in URL_LOGGERS}
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_url_levels.items():
            logging.getLogger(name).setLevel(level)


class _FakeScheduler:
    def start(self) -> None:
        logging.getLogger("app.worker").info("price_backfill_daily: 3/3 succeeded")
        logging.getLogger("app.worker").debug("debug-detail")


def _run_main(monkeypatch, level: str) -> None:
    monkeypatch.setattr(worker, "build_scheduler", lambda: _FakeScheduler())
    monkeypatch.setattr(core_config, "get_settings", lambda: SimpleNamespace(log_level=level))
    worker.main()


def test_worker_main_emits_info_job_logs(monkeypatch, capsys):
    with _unconfigured_root() as root:
        _run_main(monkeypatch, "info")
        level = root.level

    err = capsys.readouterr().err
    assert "INFO app.worker price_backfill_daily: 3/3 succeeded" in err
    assert "debug-detail" not in err
    assert level == logging.INFO


def test_worker_main_honours_log_level(monkeypatch, capsys):
    with _unconfigured_root():
        _run_main(monkeypatch, "warning")
    assert "price_backfill_daily" not in capsys.readouterr().err

    with _unconfigured_root():
        _run_main(monkeypatch, "DEBUG")
    assert "debug-detail" in capsys.readouterr().err


def test_worker_main_keeps_request_urls_with_secrets_out_of_the_log(monkeypatch, capsys):
    with _unconfigured_root():
        _run_main(monkeypatch, "debug")
        for name in URL_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        with httpx.Client(transport=transport) as client:
            client.get("https://api.example.test/bot123:SECRETTOKEN/getMe?apikey=SECRETKEY")
    assert "SECRET" not in capsys.readouterr().err


def test_configure_logging_uses_the_api_format_and_tolerates_a_bad_level(capsys):
    with _unconfigured_root() as root:
        configure_logging("not-a-level")
        level = root.level
        formatter = root.handlers[0].formatter
        logging.getLogger("quantfolio").info("hello")

    assert level == logging.INFO
    assert formatter is not None and formatter._fmt == LOG_FORMAT
    assert capsys.readouterr().err.strip().endswith("INFO quantfolio hello")
