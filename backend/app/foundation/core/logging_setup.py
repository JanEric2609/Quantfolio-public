"""Process-wide logging setup shared by the API (``app.main``) and the worker.

Both processes are entrypoints that must log the same way: same format, same
``LOG_LEVEL`` env, same suppression of URL-logging libraries. Before this
module the worker never configured logging, so every INFO line from its jobs
(``price_backfill_daily: ...``, ``rss_refresh: ...``) was dropped by the
root logger's default WARNING level.
"""
from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# These log every request URL — query-string API keys (Alpha Vantage, Finnhub,
# Twelve Data) and the Telegram bot token in the path — at INFO/DEBUG.
URL_LOGGERS = ("httpx", "httpcore", "urllib3")


def quiet_url_loggers() -> None:
    """Pin the URL-logging libraries to WARNING so secrets in URLs stay out of logs."""
    for name in URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once and silence the URL-logging libraries.

    ``level`` is a level name such as ``"info"`` (the ``LOG_LEVEL`` setting);
    an unknown name falls back to INFO rather than crashing the process.
    ``logging.basicConfig`` is a no-op when the root logger already has
    handlers (e.g. under uvicorn or pytest), which is the intended behaviour.
    """
    resolved = logging.getLevelName(str(level or "info").upper())
    if not isinstance(resolved, int):
        resolved = logging.INFO
    logging.basicConfig(level=resolved, format=LOG_FORMAT)
    quiet_url_loggers()
