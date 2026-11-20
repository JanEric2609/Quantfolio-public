"""Error results name what failed and the exception type, never the exception's text.

CodeQL traced ``str(exc)`` from these sources to API responses; the full text
now goes only to the server log.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_backbone.ingest import DataIngester
from conftest import _create_ormless_tables

SECRET_TEXT = "exception text that must stay in the server log"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    _create_ormless_tables(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _boom(*_args, **_kwargs):
    raise RuntimeError(SECRET_TEXT)


def _bare_ingester(db, registry) -> DataIngester:
    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = registry
    return ingester


def test_repull_failure_reports_only_the_exception_type():
    db = _memory_db()
    rows = [
        {"date": d.date().isoformat(), "open": c, "high": c, "low": c, "close": c, "volume": 1}
        for d, c in zip(pd.bdate_range("2025-10-20", periods=3), [100.0, 100.5, 101.0])
    ]
    registry = MagicMock()
    registry.get_price_history.return_value = {"ok": True, "provider": "yfinance", "data": rows}
    ingester = _bare_ingester(db, registry)
    ingester.split_events = lambda symbol: []  # type: ignore[method-assign]
    ingester._write_quarantine = _boom  # type: ignore[method-assign]
    result = ingester.repull_series("SPY", provider="yfinance", start_date="2025-10-20")
    assert result == {"success": False, "symbol": "SPY", "message": "repull failed (RuntimeError)", "repull": True}


def test_vix_fill_failure_reports_only_the_exception_type():
    ingester = _bare_ingester(_memory_db(), SimpleNamespace(first_success=_boom, providers=[]))
    result = ingester._fill_vix_from_index(date(2026, 11, 1), date(2026, 11, 20))
    assert result == {"added": 0, "error": "VIX fill failed (RuntimeError)"}


def test_macro_backfill_failure_reports_only_the_exception_type():
    from app.foundation.providers.fred_provider import FredProvider

    fred = FredProvider.__new__(FredProvider)
    registry = SimpleNamespace(providers=[fred])
    ingester = _bare_ingester(_memory_db(), registry)
    ingester._fetch_macro_history = _boom  # type: ignore[method-assign]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(FredProvider, "enabled", property(lambda self: True), raising=False)
        result = ingester.ingest_macro_indicators(["VIXCLS"])
    assert result["fred"] == {"success": False, "error": "macro backfill failed (RuntimeError)"}


def test_macro_ingest_failure_reports_only_the_exception_type():
    db = _memory_db()
    registry = SimpleNamespace(first_success=lambda capability: {
        "ok": True, "provider": "fred", "data": {"VIXCLS": {"value": 18.5, "timestamp": "2026-11-19T00:00:00"}},
    })
    ingester = _bare_ingester(db, registry)
    db.commit = _boom  # type: ignore[method-assign]
    result = ingester.ingest_macro_indicators()
    assert result["fred"] == {"success": False, "error": "macro ingest failed (RuntimeError)"}


def test_rss_blocked_url_message_is_fixed():
    from app.foundation.rss_provider import fetch_feed, invalidate_cache

    invalidate_cache()
    result = fetch_feed("http://127.0.0.1/feed.xml")
    assert result["ok"] is False
    assert result["error"] == "URL validation failed: not a public http(s) address"


def test_rss_feed_that_raises_is_reported_by_type(monkeypatch):
    from app.foundation import rss_provider

    monkeypatch.setattr(rss_provider, "get_feed_urls", lambda db=None: ["https://example.com/feed"])
    monkeypatch.setattr(rss_provider, "fetch_feed", _boom)
    result = rss_provider.fetch_all_feeds()
    assert result["errors"] == ["https://example.com/feed: RuntimeError"]
    assert SECRET_TEXT not in str(result)


def test_obsidian_sync_failure_message_is_fixed(monkeypatch):
    from app.foundation import obsidian, research_sync

    monkeypatch.setattr(obsidian, "obsidian_status", lambda db: {"enabled": True, "available": True})
    monkeypatch.setattr(research_sync, "generate_research_markdown", lambda db, user_id: [])
    monkeypatch.setattr("app.foundation.core.net.validate_outbound_url", _boom)
    result = research_sync.sync_to_obsidian(_memory_db(), "u1")
    assert result["synced"] is False
    assert result["message"] == "Failed to sync to Obsidian"
