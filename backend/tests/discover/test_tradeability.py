"""DKB tradeability: funds by domicile (PRIIPs), shares by listing venue.

Cases are the 2026-09-26 prod run: the index screen supplies US shares with
no ISIN and no venue suffix, and the gate rejected all 33 that reached it
(MS, C, MU, GS, JNJ, XOM, GOOGL, LLY ...), leaving a shortlist of German
shares and world ETFs that duplicate the core.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import tradeability
from app.decision.discover.tradeability import assess
from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture(autouse=True)
def _no_registry(monkeypatch):
    # The registry lookup is a live provider call; it must not decide these cases.
    monkeypatch.setattr(tradeability, "_registry_confirms_eu_venue", lambda db, symbol: False)


@pytest.mark.parametrize("symbol", ["MS", "C", "MU", "GS", "JNJ", "XOM", "GOOGL", "LLY", "BRK-B"])
def test_us_share_from_the_index_screen_is_tradeable(symbol):
    result = assess(_memory_db(), symbol, None, symbol, instrument_type="equity")

    assert result["likely_tradeable"] is True
    assert result["confidence"] == "medium"
    assert "Tradegate" in result["brokers"]["dkb"]["note"]
    assert "gettex" in result["brokers"]["scalable"]["note"]


def test_us_share_with_a_us_isin_is_tradeable():
    result = assess(_memory_db(), "AAPL", "US0378331005", "Apple Inc.", instrument_type="equity")

    assert result["likely_tradeable"] is True


def test_eu_share_keeps_high_confidence():
    result = assess(_memory_db(), "DB1.DE", None, "Deutsche Boerse", instrument_type="equity")

    assert result["likely_tradeable"] is True
    assert result["confidence"] == "high"


def test_eu_domiciled_etf_is_tradeable():
    result = assess(_memory_db(), "VWCE.DE", "IE00BK5BQT80", "Vanguard FTSE All-World", instrument_type="etf")

    assert result["likely_tradeable"] is True
    assert result["confidence"] == "high"


def test_us_domiciled_etf_is_blocked_for_want_of_a_kid():
    by_isin = assess(_memory_db(), "SPY", "US78462F1030", "SPDR S&P 500 ETF Trust", instrument_type="etf")
    by_listing = assess(_memory_db(), "QQQ", None, "Invesco QQQ Trust", instrument_type="etf")

    assert by_isin["likely_tradeable"] is False
    assert by_listing["likely_tradeable"] is False
    assert any("key information document" in r for r in by_isin["reasons"])


def test_share_listed_outside_eu_and_us_stays_uncertain():
    result = assess(_memory_db(), "7203.T", "JP3633400001", "Toyota", instrument_type="equity")

    assert result["likely_tradeable"] is False
    assert result["confidence"] == "low"


def test_non_securities_are_not_us_listings():
    assert not tradeability._is_us_listing("USDEUR=X", None)
    assert not tradeability._is_us_listing("^GSPC", None)
    assert not tradeability._is_us_listing("SAP.DE", "DE0007164600")


def test_instrument_type_defaults_to_the_pipeline_classifier():
    # EUNL.DE is in the static ETF table, so it is judged as a fund.
    result = assess(_memory_db(), "EUNL.DE", "IE00B4L5Y983", "iShares Core MSCI World")

    assert result["instrument_type"] == "etf"
    assert result["likely_tradeable"] is True


def test_dkb_link_opens_the_public_search_on_the_isin():
    """/banking/wertpapiersuche?q=<ticker> answered 404, and DKB's search
    does not resolve a ticker like "C" anyway (2026-09-28)."""
    from app.decision.discover.tradeability import dkb_search_url

    assert dkb_search_url("ie00b3wjkg14") == (
        "https://www.dkb.de/privatkunden/investieren/wertpapiersuche?isin=IE00B3WJKG14"
    )
    assert dkb_search_url(None) == "https://www.dkb.de/privatkunden/investieren/wertpapiersuche"
    assert dkb_search_url("MU") == "https://www.dkb.de/privatkunden/investieren/wertpapiersuche"


def test_a_failed_check_is_unknown_not_likely():
    from app.decision.discover.tradeability import unknown

    result = unknown(None, "boom")
    assert result["likely_tradeable"] is None
    assert result["confidence"] == "unknown"
    assert result["manual_check_url"].endswith("/wertpapiersuche")


def test_each_broker_gets_its_own_badge_and_link():
    result = assess(_memory_db(), "EUNL.DE", "IE00B4L5Y983", "iShares Core MSCI World", instrument_type="etf")

    assert result["brokers"]["dkb"]["likely"] is True and result["brokers"]["scalable"]["likely"] is True
    assert result["brokers"]["dkb"]["url"].endswith("isin=IE00B4L5Y983")
    assert result["brokers"]["scalable"]["url"] == "https://de.scalable.capital/broker/security?isin=IE00B4L5Y983"


def test_a_fund_without_a_key_information_document_is_blocked_at_both():
    result = assess(_memory_db(), "SPY", "US78462F1030", "SPDR S&P 500", instrument_type="etf")

    assert result["likely_tradeable"] is False
    assert {b["likely"] for b in result["brokers"].values()} == {False}
    assert result["brokers"]["scalable"]["confidence"] == "low"


def test_no_isin_means_no_scalable_deep_link():
    result = assess(_memory_db(), "MU", None, "Micron", instrument_type="equity")
    assert result["brokers"]["scalable"]["url"] is None
