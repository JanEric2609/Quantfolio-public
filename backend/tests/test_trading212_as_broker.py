"""Trading 212 as a place to buy: fees, the monthly plan's broker choice, Discover and the links."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import tradeability
from app.decision.monthly_plan import buy_broker
from app.foundation.broker_fees import order_fee_eur
from app.foundation.broker_instruments import APP_URL, Trading212Listings, needs_refresh, replace_instruments
from app.foundation.core.db import Base
from app.foundation.recommendation_execution import broker_links

MSCI_WORLD = "IE00B4L5Y983"
APPLE = "US0378331005"
SAP = "DE0007164600"
NOT_AT_T212 = "LU0000000003"

INSTRUMENTS = [
    {"ticker": "EUNLd_EQ", "isin": MSCI_WORLD, "type": "ETF", "currencyCode": "EUR", "name": "iShares Core MSCI World"},
    {"ticker": "IWDAl_EQ", "isin": MSCI_WORLD, "type": "ETF", "currencyCode": "USD"},
    {"ticker": "AAPL_US_EQ", "isin": APPLE, "type": "STOCK", "currencyCode": "USD", "name": "Apple"},
    {"ticker": "SAPd_EQ", "isin": SAP, "type": "STOCK", "currencyCode": "EUR"},
    {"ticker": "NOISIN_EQ", "isin": "", "type": "STOCK"},
    "not a dict",
]


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _db_with_list():
    db = _memory_db()
    assert replace_instruments(db, INSTRUMENTS) == 4
    db.commit()
    return db


AUTO = {"broker": "auto"}
ALL_THREE = ["dkb", "scalable", "trading212"]


# --- the list ---------------------------------------------------------------


def test_without_a_stored_list_every_answer_is_unknown_never_no():
    db = _memory_db()
    listings = Trading212Listings(db)
    assert listings.loaded is False
    assert listings.listed(MSCI_WORLD) is None
    assert listings.currency(MSCI_WORLD) is None
    assert needs_refresh(db)


def test_an_empty_answer_keeps_the_last_list():
    db = _db_with_list()
    assert replace_instruments(db, []) == 0
    assert replace_instruments(db, {"error": "x"}) == 0
    assert Trading212Listings(db).listed(APPLE) is True


def test_the_list_is_refreshed_once_a_day():
    db = _db_with_list()
    assert not needs_refresh(db)
    assert needs_refresh(db, now=datetime.now(UTC).replace(year=datetime.now(UTC).year + 1))


def test_a_listing_prefers_the_eur_line_and_links_only_a_us_line():
    listings = Trading212Listings(_db_with_list())
    assert listings.currency(MSCI_WORLD) == "EUR"
    assert listings.listed(NOT_AT_T212) is False
    assert listings.listed(None) is None
    assert listings.url(APPLE) == "https://www.trading212.com/trading-instruments/invest/AAPL.US"
    assert listings.url(SAP) == APP_URL


# --- fees -------------------------------------------------------------------


def test_trading212_charges_only_for_the_currency_conversion():
    assert order_fee_eur("trading212", 1_000, currency="EUR") == 0
    assert order_fee_eur("trading212", 1_000) == 0
    assert order_fee_eur("trading212", 1_000, currency="USD") == pytest.approx(1.50)
    assert order_fee_eur("trading212", 0, currency="USD") == 0
    # The other two are unchanged.
    assert order_fee_eur("scalable", 1_000) == 0.99
    assert order_fee_eur("dkb", 1_000) == 10.0


# --- the monthly plan's broker ----------------------------------------------


def test_auto_sends_an_order_to_trading212_when_it_offers_the_isin_in_eur():
    t212 = Trading212Listings(_db_with_list())
    assert buy_broker(AUTO, ALL_THREE, "order", 1_000, "Some ETF", isin=SAP, t212=t212) == "trading212"


def test_auto_never_sends_an_isin_trading212_does_not_offer_there():
    t212 = Trading212Listings(_db_with_list())
    assert buy_broker(AUTO, ALL_THREE, "order", 1_000, isin=NOT_AT_T212, t212=t212) == "scalable"
    assert buy_broker(AUTO, ["dkb", "trading212"], "order", 1_000, isin=NOT_AT_T212, t212=t212) == "dkb"
    # Without a list Trading 212 is not offered either.
    empty = Trading212Listings(_memory_db())
    assert buy_broker(AUTO, ALL_THREE, "order", 1_000, isin=SAP, t212=empty) == "scalable"


def test_the_fx_fee_can_make_another_broker_cheaper():
    t212 = Trading212Listings(_db_with_list())
    # 0.15 % of 1,000 € is 1.50 €, more than Scalable's 0.99 €.
    assert buy_broker(AUTO, ALL_THREE, "order", 1_000, isin=APPLE, t212=t212) == "scalable"
    # 0.15 % of 400 € is 0.60 €.
    assert buy_broker(AUTO, ALL_THREE, "order", 400, isin=APPLE, t212=t212) == "trading212"


def test_a_free_savings_plan_stays_at_scalable_on_a_tie():
    t212 = Trading212Listings(_db_with_list())
    assert buy_broker(AUTO, ALL_THREE, "savings_plan", 500, isin=MSCI_WORLD, t212=t212) == "scalable"
    assert buy_broker(AUTO, ["dkb", "trading212"], "savings_plan", 500, isin=MSCI_WORLD, t212=t212) == "trading212"


def test_the_owners_choice_wins():
    t212 = Trading212Listings(_db_with_list())
    assert buy_broker({"broker": "trading212"}, ["dkb"], "order", 1_000, isin=NOT_AT_T212, t212=t212) == "trading212"


# --- Discover and the links -------------------------------------------------


def test_discover_looks_trading212_up_in_its_list(monkeypatch):
    monkeypatch.setattr(tradeability, "_registry_confirms_eu_venue", lambda db, sym: False)
    db = _db_with_list()
    listed = tradeability.assess(db, "AAPL", APPLE, "Apple", "stock")["brokers"]["trading212"]
    assert listed["likely"] is True and listed["confidence"] == "high"
    assert "0.15 %" in listed["note"]
    assert listed["url"].endswith("/invest/AAPL.US")

    sap = tradeability.assess(db, "SAP.DE", SAP, "SAP", "stock")["brokers"]["trading212"]
    assert sap["url"] == APP_URL and SAP in sap["note"]

    missing = tradeability.assess(db, "XYZ.DE", NOT_AT_T212, "Fund", "etf")["brokers"]["trading212"]
    assert missing["likely"] is False

    no_isin = tradeability.assess(db, "XYZ.DE", None, "Fund", "etf")
    assert no_isin["brokers"]["trading212"]["confidence"] == "unknown"
    assert no_isin["confidence"] == "high"  # the unknown badge does not break the overall rating


def test_discover_has_no_trading212_badge_before_the_first_sync(monkeypatch):
    monkeypatch.setattr(tradeability, "_registry_confirms_eu_venue", lambda db, sym: False)
    result = tradeability.assess(_memory_db(), "AAPL", APPLE, "Apple", "stock")
    assert set(result["brokers"]) == {"dkb", "scalable"}


def test_a_trading212_link_only_for_an_isin_it_offers():
    db = _db_with_list()
    assert [link["broker"] for link in broker_links(APPLE, db)] == ["dkb", "scalable", "trading212"]
    assert [link["broker"] for link in broker_links(NOT_AT_T212, db)] == ["dkb", "scalable"]
    assert [link["broker"] for link in broker_links(APPLE)] == ["dkb", "scalable"]
