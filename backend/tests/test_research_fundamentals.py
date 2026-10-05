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
