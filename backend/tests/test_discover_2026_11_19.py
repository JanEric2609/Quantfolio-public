"""Findings from Discover run b5a132a4 (2026-11-19).

The run left out six Tel Aviv names (agorot had no EUR rate; covered in
tests/discover/test_universe.py), listed an ETF as "<NA>.DE", ran the estimate
and the cash return together in a thesis, and fetched 240 fundamentals live
because they expired between the nightly prefetch and the run.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover.thesis_checks import tidy_sentence
from app.foundation import market
from app.foundation.core.db import Base
from app.foundation.etf_universe import _to_xetra_symbol
from app.foundation.models.entities import Fundamental


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.mark.parametrize("missing", [None, float("nan"), pd.NA, pd.NaT, "", "  ", "nan", "<NA>", "None"])
def test_a_missing_ticker_and_wkn_give_no_symbol(missing):
    assert _to_xetra_symbol(missing, missing, "LU3003218107") is None


def test_the_wkn_stands_in_for_a_missing_ticker():
    assert _to_xetra_symbol(pd.NA, "dbx0a1", "LU0000000000") == "DBX0A1.DE"
    assert _to_xetra_symbol("eunl", pd.NA, "IE00B4L5Y983") == "EUNL.DE"


def test_the_estimate_and_the_cash_return_get_a_word_between_them():
    cve = ("CVE trades at a trailing price-to-earnings ratio of 8.6, while offering +2.7 % over 6 months "
           "(the system's estimate) 1.22 % from cash over the same months.")
    assert "(the system's estimate) against 1.22 % from cash over the same months" in tidy_sentence(cve)


def test_a_sentence_that_already_compares_them_is_left_alone():
    ok = "+2.7 % over 6 months (the system's estimate) against 1.22 % from cash over the same months."
    assert tidy_sentence(ok) == ok


class _Registry:
    def __init__(self):
        self.calls = 0

    def get_fundamentals(self, ticker):
        self.calls += 1
        return {"ok": True, "provider": "yfinance",
                "data": {"market_cap": 1e10, "pe": 9.0, "roe": 0.2, "ebitda": 1e9}}


def _cached(db, age):
    db.add(Fundamental(ticker="CVE.TO", data_json=json.dumps({"pe": 8.6, "ebitda": 1e9}), source="yfinance",
                       fetched_at=datetime.now(UTC) - age, stale=False))
    db.commit()


def test_a_row_expiring_within_the_refresh_window_is_fetched_again():
    db = _memory_db()
    _cached(db, timedelta(days=6, hours=15))  # expires in 9 hours, before the morning run
    reg = _Registry()
    with patch.object(market, "build_provider_registry", lambda db: reg):
        assert market.fundamentals(db, "CVE.TO")["data"]["pe"] == 8.6
        assert reg.calls == 0
        assert market.fundamentals(db, "CVE.TO", refresh_ahead=timedelta(days=1))["data"]["pe"] == 9.0
    assert reg.calls == 1


def test_a_row_with_more_than_a_day_left_is_served_from_the_cache():
    db = _memory_db()
    _cached(db, timedelta(days=3))
    reg = _Registry()
    with patch.object(market, "build_provider_registry", lambda db: reg):
        assert market.fundamentals(db, "CVE.TO", refresh_ahead=timedelta(days=1))["data"]["pe"] == 8.6
    assert reg.calls == 0
