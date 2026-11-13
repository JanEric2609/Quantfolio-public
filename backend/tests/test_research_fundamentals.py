"""Research reads fundamentals through the refetching cache, not the raw table."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.foundation.market as market
from app.foundation.core.db import Base
from app.foundation.models.entities import Fundamental
from app.foundation.research import _latest_fundamentals


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _Registry:
    def __init__(self, result):
        self._result = result

    def get_fundamentals(self, ticker):
        return self._result


def _stale_exi2(db):
    # As migration 0113 left it on prod: backdated, flagged, in the old units.
    db.add(Fundamental(
        ticker="EXI2.DE", data_json=json.dumps({"dividend_yield": 0.34, "pe_ratio": 26.5}),
        fetched_at=datetime(2000, 1, 1, tzinfo=UTC), source="yfinance", stale=True,
    ))
    db.commit()


def test_a_stale_row_is_refetched_before_research_sees_it(monkeypatch):
    db = _memory_db()
    _stale_exi2(db)
    fresh = {"ok": True, "provider": "yfinance", "data": {"dividend_yield": 0.0034, "pe_ratio": 26.9}}
    monkeypatch.setattr(market, "build_provider_registry", lambda _db: _Registry(fresh))

    assert _latest_fundamentals(db, "EXI2.DE")["dividend_yield"] == 0.0034


def test_the_cached_row_is_still_served_when_the_provider_is_down(monkeypatch):
    db = _memory_db()
    _stale_exi2(db)
    monkeypatch.setattr(market, "build_provider_registry", lambda _db: _Registry({"ok": False, "error": "down"}))

    assert _latest_fundamentals(db, "EXI2.DE")["pe_ratio"] == 26.5


def test_nothing_cached_and_nothing_live_is_none(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(market, "build_provider_registry", lambda _db: _Registry({"ok": False}))

    assert _latest_fundamentals(db, "NOPE") is None


def test_unknown_ticker_payload_has_no_content_and_stores_no_row():
    from unittest.mock import patch

    from app.foundation import market
    from app.foundation.models.entities import Fundamental

    assert not market.has_fundamentals_content({"pe_ratio": None, "roe": None, "growth_basis": "quarter_yoy", "name": None})
    assert market.has_fundamentals_content({"pe_ratio": 12.0})

    db = _memory_db()
    empty = {"ok": True, "provider": "yfinance", "data": {"pe_ratio": None, "growth_basis": "quarter_yoy"}}

    class _Registry:
        def get_fundamentals(self, symbol):
            return empty

    with patch.object(market, "build_provider_registry", return_value=_Registry()):
        result = market.fundamentals(db, "ANDG.US")
    assert result["source"] == "unavailable"
    assert db.query(Fundamental).count() == 0


def test_unknown_ticker_gets_no_verdict_call():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.foundation import research

    assert not research.has_research_data({"fundamentals": {"pe_ratio": None}, "price_history": [{}] * 10})
    assert research.has_research_data({"fundamentals": None, "price_history": [{}] * 260})

    db = _memory_db()
    llm = AsyncMock()
    with patch.object(research, "build_context", return_value={"fundamentals": None, "price_history": []}), \
            patch("app.foundation.llm.router.call", llm):
        out = asyncio.run(research.generate_multi_horizon_verdicts(db, "ANDG.US", "u1"))
    assert out["verdicts"] == [] and out["error"] == "not_found"
    llm.assert_not_called()


def test_yfinance_empty_info_is_not_found():
    import sys
    import types
    from unittest.mock import patch

    from app.foundation.providers.yfinance_provider import YFinanceProvider

    fake = types.SimpleNamespace(Ticker=lambda sym: types.SimpleNamespace(info={}))
    with patch.dict(sys.modules, {"yfinance": fake}):
        result = YFinanceProvider().get_fundamentals("ANDG.US")
    assert result["ok"] is False


def test_52_week_range_uses_daily_highs_and_lows_not_closes():
    from app.foundation.research import price_technicals

    rows = [{"date": f"2026-01-{d:02d}", "open": 10, "high": 10 + d, "low": 9, "close": 10.0, "volume": 1} for d in range(1, 11)]
    rows[3]["low"] = 7.5
    out = price_technicals(rows, today=__import__("datetime").date(2026, 6, 1))
    assert out["high_52w"] == 20.0 and out["high_52w_date"] == "2026-01-10"
    assert out["low_52w"] == 7.5 and out["low_52w_date"] == "2026-01-04"


def test_report_news_falls_back_to_gdelt_then_on_demand_then_none():
    from datetime import UTC, date, datetime
    from unittest.mock import patch

    from app.foundation import research
    from app.foundation.models.entities import NewsMention

    db = _memory_db()
    with patch.object(research, "_on_demand_news", return_value=[]):
        assert research._news_with_coverage(db, "WKL.AS") == ([], "none")
    with patch.object(research, "_on_demand_news", return_value=[{"title": "Live headline"}]):
        assert research._news_with_coverage(db, "WKL.AS") == ([{"title": "Live headline"}], "on_demand")
    db.add(NewsMention(symbol="WKL.AS", day=date(2026, 11, 10), seen_at=datetime(2026, 11, 10, tzinfo=UTC),
                       url="http://x", title="GDELT headline", title_key="gdelt headline"))
    db.commit()
    rows, how = research._news_with_coverage(db, "WKL.AS")
    assert how == "gdelt" and rows[0]["title"] == "GDELT headline" and rows[0]["sentiment_label"] is None


def test_no_news_prompt_says_no_source_covers_the_company():
    from app.foundation import research

    ctx = {"ticker": "X", "recent_news": [], "fundamentals": None, "technicals": None, "quote": {}, "recommendations": []}
    text = research._build_user_message(ctx)
    assert "no news source covers this company" in text
