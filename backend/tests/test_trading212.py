"""Trading 212 read-only sync: client guards, rate limits, paging, sync, ledger, tax, pies.

No request leaves the process: a fake transport answers each allow-listed GET
with JSON shaped like the API's OpenAPI schemas, so everything from the URL to
the ledger rows is real except the HTTP call.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import trading212_status
from app.foundation.core.db import Base
from app.foundation.live_positions import combined_positions, synced_cash
from app.foundation.models.entities import (
    ActivityLedgerEntry,
    ApiKey,
    BrokerPie,
    BrokerPosition,
    ConnectedAccount,
    TaxLot,
    User,
)
from app.foundation.settings import get_public_settings, get_secret, set_secret, upsert_public_settings
from app.foundation.trading212 import client as t212_client
from app.foundation.trading212 import service as t212
from app.foundation.trading212.client import (
    ALLOWED_PATHS,
    Trading212Client,
    Trading212Config,
    Trading212ReadOnlyViolation,
    check_path,
)
from app.foundation.trading212.mapping import map_orders


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


CMG = "US1696561059"
VHYL = "IE00B8GKDB10"
EARLIER = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
RECENT = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
LATER = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _order(fill_id: int, isin: str, ticker: str, side: str, qty: str, price: str, net: str, *,
           currency: str = "EUR", fx: str = "1", fees: list[dict] | None = None, when: str = EARLIER,
           fill_type: str = "TRADE") -> dict:
    return {
        "order": {"id": fill_id + 1000, "status": "FILLED", "side": side, "type": "MARKET", "ticker": ticker,
                  "currency": currency,
                  "instrument": {"isin": isin, "ticker": ticker, "name": ticker.split("_")[0], "currency": currency}},
        "fill": {"id": fill_id, "type": fill_type, "filledAt": when, "price": float(price), "quantity": float(qty),
                 "walletImpact": {"currency": "EUR", "netValue": float(net), "fxRate": float(fx),
                                  "realisedProfitLoss": 0, "taxes": fees or []}},
    }


def _api() -> dict[str, Any]:
    return {
        "/equity/account/summary": {
            "id": 4711, "currency": "EUR", "totalValue": 1475.0,
            "cash": {"availableToTrade": 300.0, "inPies": 5.0, "reservedForOrders": 0.0},
            "investments": {"currentValue": 1170.0, "totalCost": 1100.0},
        },
        "/equity/positions": [
            {"instrument": {"isin": CMG, "ticker": "CMG_US_EQ", "name": "Chipotle", "currency": "USD"},
             "quantity": 10, "quantityInPies": 0, "averagePricePaid": 55.0, "currentPrice": 52.0,
             "walletImpact": {"currency": "EUR", "currentValue": 470.0, "totalCost": 500.0}},
            {"instrument": {"isin": VHYL, "ticker": "VHYLl_EQ", "name": "Vanguard All-World High Div",
                            "currency": "EUR"},
             "quantity": 10, "quantityInPies": 10, "averagePricePaid": 60.0, "currentPrice": 70.0,
             "walletImpact": {"currency": "EUR", "currentValue": 700.0, "totalCost": 600.0}},
        ],
        "/equity/history/orders": [
            [_order(2, VHYL, "VHYLl_EQ", "BUY", "10", "60", "600"),
             _order(1, CMG, "CMG_US_EQ", "BUY", "10", "55", "501",
                    currency="USD", fx="1.1", fees=[{"name": "CURRENCY_CONVERSION_FEE", "quantity": -1.0,
                                                     "currency": "EUR"}])],
        ],
        "/equity/history/dividends": [[
            {"reference": "div-1", "amount": 4.0, "amountInEuro": 4.0, "currency": "EUR",
             "grossAmountPerShare": 0.5, "quantity": 10, "paidOn": RECENT, "type": "ORDINARY",
             "ticker": "VHYLl_EQ", "tickerCurrency": "EUR",
             "instrument": {"isin": VHYL, "ticker": "VHYLl_EQ", "name": "Vanguard", "currency": "EUR"}},
        ]],
        "/equity/history/transactions": [[
            {"reference": "dep-1", "type": "DEPOSIT", "amount": 1400.0, "currency": "EUR", "dateTime": EARLIER},
            {"reference": "int-1", "type": "INTEREST_ON_FREE_CASH", "amount": 1.0, "currency": "EUR",
             "dateTime": RECENT},
        ]],
        "/equity/pies": [{"id": 77, "cash": 5.0, "status": "ON_TRACK",
                          "result": {"priceAvgInvestedValue": 600, "priceAvgValue": 700, "priceAvgResult": 100,
                                     "priceAvgResultCoef": 0.1667},
                          "dividendDetails": {"gained": 4, "reinvested": 0, "inCash": 4}}],
        "/equity/pies/77": {
            "instruments": [{"ticker": "VHYLl_EQ", "expectedShare": 1, "currentShare": 1, "ownedQuantity": 10,
                             "result": {"priceAvgValue": 700, "priceAvgResult": 100}, "issues": []}],
            "settings": {"id": 77, "name": "Dividends", "dividendCashAction": "TO_ACCOUNT_CASH", "goal": 1000},
        },
        "/equity/metadata/instruments": [
            {"isin": CMG, "ticker": "CMG_US_EQ", "type": "STOCK", "currencyCode": "USD", "name": "Chipotle"},
            {"isin": VHYL, "ticker": "VHYLl_EQ", "type": "ETF", "currencyCode": "USD"},
            {"isin": VHYL, "ticker": "VGWDd_EQ", "type": "ETF", "currencyCode": "EUR"},
        ],
    }


class FakeApi:
    """Answers allow-listed GETs; feeds given as a list of pages are paged by cursor."""

    def __init__(self, data: dict[str, Any], *, fail: dict[str, int] | None = None) -> None:
        self.data = data
        self.fail = fail or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], params: dict[str, Any],
                 timeout: float) -> httpx.Response:
        path = url.split("/api/v0", 1)[1]
        self.calls.append((method, path, dict(params)))
        assert method == "GET"
        assert headers["Authorization"].startswith("Basic ")
        rl = {"x-ratelimit-remaining": "5", "x-ratelimit-reset": "0", "x-ratelimit-limit": "6"}
        if path in self.fail:
            return httpx.Response(self.fail[path], json={}, headers=rl)
        body = self.data[path]
        if isinstance(body, list) and body and isinstance(body[0], list):
            page = int(params.get("cursor") or 0)
            nxt = f"/api/v0{path}?limit=50&cursor={page + 1}" if page + 1 < len(body) else None
            body = {"items": body[page], "nextPagePath": nxt}
        return httpx.Response(200, json=body, headers=rl)


@pytest.fixture
def fake(monkeypatch):
    api = FakeApi(_api())

    def factory(config: Trading212Config) -> Trading212Client:
        return Trading212Client(config, transport=api, sleep=lambda _s: None)

    t212.set_client_factory(factory)
    monkeypatch.setattr("app.foundation.broker_sync.post_sync_steps",
                        lambda db, user_id, *, tax_step, label: {"post_steps": {"tax": tax_step()}})
    yield api
    t212.set_client_factory(None)


def _setup(db) -> User:
    user = User(username="owner", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    set_secret(db, "trading212", "KEY-123", meta={"api_secret": "SECRET-456"})
    upsert_public_settings(db, {"trading212_enabled": True})
    return user


# --- the client cannot write -------------------------------------------------


def test_only_get_paths_on_the_allow_list_are_ever_built():
    assert ALLOWED_PATHS == {
        "/equity/account/summary", "/equity/positions", "/equity/history/orders", "/equity/history/dividends",
        "/equity/history/transactions", "/equity/pies", "/equity/metadata/instruments",
    }
    assert check_path("/equity/pies/12") == "/equity/pies/12"
    for path in ("/equity/orders", "/equity/orders/market", "/equity/orders/1", "/equity/history/exports",
                 "/equity/pies/12/duplicate", "/equity/pies/../orders", "/equity/positions?x=1"):
        with pytest.raises(Trading212ReadOnlyViolation):
            check_path(path)
    assert not hasattr(Trading212Client, "post") and not hasattr(Trading212Client, "delete")


def test_a_next_page_outside_the_feed_is_refused():
    api = FakeApi({"/equity/history/orders": {"items": [{"x": 1}], "nextPagePath": "/api/v0/equity/orders?cursor=1"}})
    client = Trading212Client(Trading212Config("k", "s"), transport=api, sleep=lambda _s: None)
    with pytest.raises(Trading212ReadOnlyViolation):
        client.paginate("/equity/history/orders")
    api.data["/equity/history/orders"]["nextPagePath"] = "https://evil.example/api/v0/equity/history/orders?cursor=1"
    with pytest.raises(Trading212ReadOnlyViolation):
        client.paginate("/equity/history/orders")


def test_pagination_follows_the_cursor_to_the_end():
    pages = [[{"id": i} for i in range(50)], [{"id": 50 + i} for i in range(50)], [{"id": 100}]]
    api = FakeApi({"/equity/history/transactions": pages})
    client = Trading212Client(Trading212Config("k", "s"), transport=api, sleep=lambda _s: None)
    items, cut = client.paginate("/equity/history/transactions")
    assert len(items) == 101 and not cut
    assert [c[2].get("cursor") for c in api.calls] == [None, "1", "2"]
    assert all(c[2]["limit"] in (50, "50") for c in api.calls)


def test_rate_limit_waits_for_the_reset_and_retries_a_429():
    slept: list[float] = []
    responses = [
        httpx.Response(200, json={}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}),
        httpx.Response(429, json={}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1060"}),
        httpx.Response(200, json={"ok": 1}, headers={"x-ratelimit-remaining": "3", "x-ratelimit-reset": "1100"}),
    ]
    client = Trading212Client(Trading212Config("k", "s"), transport=lambda *a: responses.pop(0),
                              sleep=slept.append, clock=lambda: 1000.0)
    client.get("/equity/account/summary")
    assert client.get("/equity/account/summary") == {"ok": 1}
    # Waited for the first window's reset, then for the 429's.
    assert slept[0] == pytest.approx(30.25) and slept[1] == pytest.approx(60.25)


def test_the_api_secret_is_stored_encrypted_not_in_meta(fake):
    db = _memory_db()
    _setup(db)
    row = db.query(ApiKey).filter(ApiKey.service == "trading212").one()
    assert "SECRET-456" not in (row.meta_json or "") and "SECRET-456" not in row.key_encrypted
    key, meta = get_secret(db, "trading212") or ("", {})
    assert key == "KEY-123" and meta["api_secret"] == "SECRET-456"


# --- sync ------------------------------------------------------------------


def test_sync_writes_positions_cash_ledger_and_pies(fake):
    db = _memory_db()
    user = _setup(db)
    log = t212.sync(db, user.id)
    assert log["state"] == "success", log["message"]
    assert {c[0] for c in fake.calls} == {"GET"}

    pos = {p.isin: p for p in db.query(BrokerPosition).filter(BrokerPosition.source == "trading212")}
    assert set(pos) == {CMG, VHYL}
    # Stored in EUR from T212's own valuation, not the USD quote.
    assert pos[CMG].current_value == Decimal("470") and pos[CMG].current_price == Decimal("47")
    assert pos[CMG].avg_buy_price == Decimal("50") and pos[CMG].currency == "EUR"
    assert pos[CMG].security_type == "STOCK" and pos[VHYL].security_type == "ETF"
    assert pos[CMG].ticker is None  # T212's CMG_US_EQ is never an app ticker

    cash, any_synced = synced_cash(db, user.id)
    assert any_synced and cash == Decimal("305")  # free cash plus cash held in pies
    assert {c.isin for c in combined_positions(db, user.id)} == {CMG, VHYL}

    rows = {r.external_id: r for r in db.query(ActivityLedgerEntry).filter(ActivityLedgerEntry.source == "trading212")}
    assert rows["fill:1"].activity_type == "buy" and rows["fill:1"].amount == Decimal("-501")
    assert rows["fill:1"].fees == Decimal("1")
    assert rows["fill:1"].price == Decimal("50")  # 55 USD at the fill's own rate
    assert rows["fill:1"].description == "Buy CMG"  # reads like Scalable's "Buy EUNL" in the activity list
    assert rows["dividend:div-1"].activity_type == "dividend" and rows["dividend:div-1"].amount == Decimal("4")
    assert json.loads(rows["dividend:div-1"].raw_json)["gross"] == "5.0"
    # The deposit before the first sync is history: the opening balance holds it.
    assert rows["cash:dep-1"].activity_type == "external_pre_sync"
    assert rows["cash:int-1"].activity_type == "interest"
    assert rows["opening:4711"].activity_type == "cashflow" and rows["opening:4711"].amount == Decimal("1475")

    pie = db.query(BrokerPie).one()
    assert pie.name == "Dividends" and pie.value == Decimal("700") and pie.dividend_action == "TO_ACCOUNT_CASH"
    assert json.loads(pie.instruments_json)[0]["isin"] == VHYL

    status = trading212_status.status(db, user.id)
    assert status["state"] == "ready" and status["positions"] == 2 and status["pies"] == 1
    # 1 EUR in 30 days on 305 EUR of cash, annualised.
    assert Decimal(status["interest_rate_measured"]) == pytest.approx(Decimal("0.0399"), abs=Decimal("0.0001"))


def test_tax_lots_carry_the_fee_and_the_eur_cost(fake):
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    lots = {lot.isin: lot for lot in db.query(TaxLot).filter(TaxLot.source == "trading212_sync")}
    assert set(lots) == {CMG, VHYL}
    # 501 EUR left the account; 10 x 50 EUR + 1 EUR fee: the fee is already in it.
    assert lots[CMG].cost_basis_eur == Decimal("501") and lots[CMG].fees_eur == Decimal("1")
    assert lots[CMG].fund_class == "other"  # a share: no Teilfreistellung


def test_a_second_sync_writes_no_duplicates_and_stops_at_known_rows(fake):
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    before = db.query(ActivityLedgerEntry).count()
    fake.data["/equity/history/orders"] = [
        [_order(3, CMG, "CMG_US_EQ", "SELL", "2", "60", "108", currency="USD", fx="1.1", when=LATER),
         _order(2, VHYL, "VHYLl_EQ", "BUY", "10", "60", "600")],
        [_order(1, CMG, "CMG_US_EQ", "BUY", "10", "55", "501", currency="USD", fx="1.1")],
    ]
    fake.calls.clear()
    log = t212.sync(db, user.id)
    assert log["state"] == "success", log["message"]
    assert db.query(ActivityLedgerEntry).count() == before + 1
    # The first page already held a stored fill, so page 2 was never asked for.
    assert [c for c in fake.calls if c[1] == "/equity/history/orders" and c[2].get("cursor")] == []


def test_a_feed_served_oldest_first_is_read_to_its_end(fake):
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    old = _order(1, CMG, "CMG_US_EQ", "BUY", "10", "55", "501", currency="USD", fx="1.1")
    new = _order(3, CMG, "CMG_US_EQ", "SELL", "2", "60", "108", currency="USD", fx="1.1", when=LATER)
    fake.data["/equity/history/orders"] = [[old, _order(2, VHYL, "VHYLl_EQ", "BUY", "10", "60", "600", when=RECENT)],
                                           [new]]
    t212.sync(db, user.id)
    assert db.query(ActivityLedgerEntry).filter(ActivityLedgerEntry.external_id == "fill:3").count() == 1
    sale = db.query(ActivityLedgerEntry).filter(ActivityLedgerEntry.external_id == "fill:3").one()
    assert sale.activity_type == "sell" and sale.amount == Decimal("108")


def test_a_split_is_a_share_movement_for_review_not_a_trade(fake):
    db = _memory_db()
    user = _setup(db)
    fake.data["/equity/history/orders"][0].append(
        _order(9, CMG, "CMG_US_EQ", "BUY", "490", "0", "0", currency="USD", fill_type="STOCK_SPLIT"))
    log = t212.sync(db, user.id)
    row = db.query(ActivityLedgerEntry).filter(ActivityLedgerEntry.external_id == "fill:9").one()
    assert row.activity_type == "corporate_action" and row.review_state == "needs_review"
    assert "share movements without a trade" in log["message"]


def test_pies_failing_never_fails_the_sync_and_keeps_the_last_read(fake):
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    fake.fail["/equity/pies"] = 403
    log = t212.sync(db, user.id)
    assert log["state"] == "success"
    assert "pies not readable" in log["message"]
    assert db.query(BrokerPie).count() == 1  # the last good read stays
    status = trading212_status.status(db, user.id)
    assert status["pies_unavailable"] and not status["pies_stale"]


def test_a_refused_key_ends_as_a_warning_and_names_the_state(fake):
    db = _memory_db()
    user = _setup(db)
    fake.fail["/equity/account/summary"] = 401
    with pytest.raises(t212_client.Trading212AuthError):
        t212.sync(db, user.id)
    assert trading212_status.status(db, user.id)["state"] == "key_invalid"


def test_probe_names_the_missing_permission(fake):
    db = _memory_db()
    _setup(db)
    fake.fail["/equity/history/orders"] = 403
    result = t212.probe(db)
    assert not result["ok"] and "History" in result["message"]
    assert [s["name"] for s in result["steps"]] == ["account", "portfolio", "history"]


def test_another_user_cannot_sync_the_owners_key(fake):
    db = _memory_db()
    owner = _setup(db)
    t212.sync(db, owner.id)
    other = User(username="other", password_hash="x", role="admin")
    db.add(other)
    db.commit()
    with pytest.raises(t212.Trading212OwnedByOtherUser):
        t212.sync(db, other.id)


def test_fx_rate_direction_is_read_from_the_booked_value():
    item = _order(1, CMG, "CMG_US_EQ", "BUY", "10", "55", "500", currency="USD", fx="1.1")
    assert map_orders([item])[0].price_in_account_currency() == Decimal("50")
    item = _order(1, CMG, "CMG_US_EQ", "BUY", "10", "55", "500", currency="USD", fx=str(1 / 1.1))
    assert map_orders([item])[0].price_in_account_currency() == pytest.approx(Decimal("50"))


def test_the_tax_plan_lists_trading212_only_once_it_is_used(fake):
    from app.foundation.tax_allowances import compute_allowance_plan

    db = _memory_db()
    user = _setup(db)
    banks = [b["bank"] for b in compute_allowance_plan(db, user.id, 2026)["banks"]]
    assert banks == ["dkb", "scalable"]
    t212.sync(db, user.id)
    plan = compute_allowance_plan(db, user.id, datetime.now(UTC).year)
    t212_row = next(b for b in plan["banks"] if b["bank"] == "trading212")
    assert t212_row["label"] == "Trading 212"
    assert plan["estimate"] is True and plan["not_tax_advice"] is True


def test_control_center_names_a_refused_key(fake):
    from app.foundation.control_center import attention_items

    db = _memory_db()
    user = _setup(db)
    fake.fail["/equity/account/summary"] = 401
    with pytest.raises(t212_client.Trading212AuthError):
        t212.sync(db, user.id)
    ids = {item["id"] for item in attention_items(db)}
    assert "trading212-key" in ids


def test_job_skips_without_an_owner_or_when_disabled():
    from app.foundation.trading212.jobs import trading212_sync_once

    db = _memory_db()
    assert trading212_sync_once(db)["reason"] == "disabled"
    upsert_public_settings(db, {"trading212_enabled": True})
    assert trading212_sync_once(db)["reason"] == "no_owner"
    assert db.query(ConnectedAccount).count() == 0


def test_the_monthly_plan_counts_trading212_free_cash_as_buying_power(fake):
    from app.decision.monthly_plan import cash_state, connected_brokers

    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    state = cash_state(db, user.id, {"emergency_reserve_eur": 0})
    depot = db.query(ConnectedAccount).filter_by(source="trading212", account_type="depot").one()
    free = json.loads(depot.raw_json)["cash"]["buying_power"]
    # Free cash is money to invest there; cash already in a pie is not.
    assert state["broker_cash_by_broker"]["trading212"] == pytest.approx(float(free))
    assert state["savings_eur"] == 0
    assert connected_brokers(db, user.id) == ["trading212"]


def test_the_sync_stores_the_instrument_list_once_a_day(fake):
    from app.foundation.broker_instruments import Trading212Listings
    from app.foundation.models.entities import BrokerInstrument

    db = _memory_db()
    user = _setup(db)
    log = t212.sync(db, user.id)
    assert log["counts"]["instruments"] == 3
    listings = Trading212Listings(db)
    assert listings.listed(VHYL) is True
    assert listings.currency(VHYL) == "EUR"  # the Xetra line beats the London one
    assert listings.currency(CMG) == "USD"
    assert listings.listed("IE00B4L5Y983") is False
    assert listings.url(CMG) == "https://www.trading212.com/trading-instruments/invest/CMG.US"
    # The positions' security types come from the stored list.
    depot = db.query(ConnectedAccount).filter_by(source="trading212", account_type="depot").one()
    assert json.loads(depot.raw_json)["security_types"] == {CMG: "STOCK", VHYL: "ETF"}

    log = t212.sync(db, user.id)
    assert "instruments" not in log["counts"]
    assert db.query(BrokerInstrument).count() == 3


def test_a_pie_list_that_is_not_a_list_keeps_the_last_read(fake):
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    fake.data["/equity/pies"] = {"items": []}
    log = t212.sync(db, user.id)
    assert log["state"] == "success" and "pies not readable" in log["message"]
    assert db.query(BrokerPie).count() == 1


def test_a_key_for_another_account_is_refused(fake):
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    fake.data["/equity/account/summary"] = {**fake.data["/equity/account/summary"], "id": 999}
    with pytest.raises(t212.Trading212AccountChanged):
        t212.sync(db, user.id)
    assert db.query(ConnectedAccount).filter(ConnectedAccount.account_type == "depot").count() == 1


def test_a_feed_past_the_page_cap_is_marked_read_and_named(fake, monkeypatch):
    monkeypatch.setattr(t212_client, "MAX_PAGES", 1)
    db = _memory_db()
    user = _setup(db)
    fake.data["/equity/history/transactions"] = [fake.data["/equity/history/transactions"][0], []]
    log = t212.sync(db, user.id)
    assert log["state"] == "warning" and "older history is not imported" in log["message"]
    depot = trading212_status.depot_account(db, user.id)
    assert json.loads(depot.raw_json)["history_complete"]["transactions"] is True


def test_trading212_dividends_are_not_grossed_up_twice():
    from datetime import date

    from app.foundation.models.entities import TaxLedgerEvent
    from app.foundation.tax_allowances import _is_net_credit

    dividend = TaxLedgerEvent(event_type="dividend", source="trading212_sync", gross_eur=Decimal("5"),
                              withheld_eur=Decimal("0"), event_date=date(2026, 10, 1), tax_year=2026)
    interest = TaxLedgerEvent(event_type="interest", source="trading212_sync", gross_eur=Decimal("1"),
                              withheld_eur=Decimal("0"), event_date=date(2026, 10, 1), tax_year=2026)
    assert not _is_net_credit(dividend)  # booked at its own gross already
    assert _is_net_credit(interest)


def test_another_user_cannot_change_the_trading212_settings_or_key(fake):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    owner = _setup(db)
    t212.sync(db, owner.id)
    other = User(username="other", password_hash="x", role="admin")
    db.add(other)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: other
    try:
        client = TestClient(app)
        off = client.put("/api/settings", json={"settings": {"trading212_enabled": False}, "integrations": {}})
        key = client.put("/api/settings", json={"settings": {},
                                                 "integrations": {"trading212": {"secret": "MINE"}}})
    finally:
        app.dependency_overrides.clear()
    assert off.status_code == 403 and key.status_code == 403
    assert (get_secret(db, "trading212") or ("", {}))[0] == "KEY-123"


def test_saving_the_trading212_key_switches_the_sync_on_unless_the_save_sets_the_switch():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = User(username="owner", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        saved = client.put("/api/settings", json={"settings": {},
                                                   "integrations": {"trading212": {"secret": "KEY", "api_secret": "S"}}})
        assert saved.status_code == 200
        assert get_public_settings(db)["trading212_enabled"] is True
        # Meta alone (no new key) and an explicit off both leave the switch to the owner.
        client.put("/api/settings", json={"settings": {"trading212_enabled": False}, "integrations": {}})
        client.put("/api/settings", json={"settings": {}, "integrations": {"trading212": {"api_secret": "S2"}}})
        assert get_public_settings(db)["trading212_enabled"] is False
        client.put("/api/settings", json={"settings": {"trading212_enabled": False},
                                          "integrations": {"trading212": {"secret": "KEY2"}}})
        assert get_public_settings(db)["trading212_enabled"] is False
    finally:
        app.dependency_overrides.clear()


def test_savings_plan_freshness_reads_each_brokers_own_log(fake):
    from app.decision.monthly_plan import plans_freshness
    from app.foundation.models.entities import BrokerSyncLog

    db = _memory_db()
    user = _setup(db)
    db.add(BrokerSyncLog(user_id=user.id, source="scalable", trigger="scheduled", state="warning",
                         started_at=datetime.now(UTC) - timedelta(hours=2),
                         counts_json=json.dumps({"plans_unavailable": True})))
    db.commit()
    t212.sync(db, user.id)  # a newer Trading 212 log must not hide the failed fetch
    assert plans_freshness(db, user.id)["last_fetch_failed"] is True


def test_removing_the_synced_data_lets_another_account_sync(fake):
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)
    fake.data["/equity/account/summary"] = {**fake.data["/equity/account/summary"], "id": 999}
    with pytest.raises(t212.Trading212AccountChanged):
        t212.sync(db, user.id)
    other = User(username="other", password_hash="x", role="admin")
    db.add(other)
    db.commit()
    removed = t212.reset_synced_data(db, user.id)
    assert removed["positions"] == 2 and removed["tax_lots"] == 2 and removed["accounts"] == 1
    # The key stays bound to its owner, and the mirror dropped the old holdings.
    assert trading212_status.owner_user_id(db) == user.id
    with pytest.raises(t212.Trading212OwnedByOtherUser):
        t212.sync(db, other.id)
    from app.foundation.models.entities import Holding
    assert db.query(Holding).filter(Holding.source == "broker_sync").count() == 0
    assert db.query(ActivityLedgerEntry).filter(ActivityLedgerEntry.source == "trading212").count() == 0
    assert get_secret(db, "trading212") is not None  # the key stays
    log = t212.sync(db, user.id)
    assert log["state"] == "success"
    assert trading212_status.depot_account(db, user.id).external_id == "999"


# --- savings plans, read back from the orders they placed -------------------


def _ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scheduled(fill_id: int, isin: str, ticker: str, net: str, days_ago: int, origin: str) -> dict:
    item = _order(fill_id, isin, ticker, "BUY", "1", net, net, when=_ago(days_ago))
    item["order"]["initiatedFrom"] = origin
    return item


def test_savings_plans_are_read_back_from_the_orders_their_schedule_placed(fake):
    from app.decision.monthly_plan import plan_settings, running_savings_plans

    fake.data["/equity/history/orders"] = [[
        # The pie "Dividends" auto-invests VHYL monthly; the last run was 50 €.
        _scheduled(10, VHYL, "VHYLl_EQ", "40", 65, "AUTOINVEST"),
        _scheduled(11, VHYL, "VHYLl_EQ", "40", 35, "AUTOINVEST"),
        _scheduled(12, VHYL, "VHYLl_EQ", "50", 5, "AUTOINVEST"),
        # A one-instrument plan that has run once: listed, schedule not known yet.
        _scheduled(13, CMG, "CMG_US_EQ", "25", 3, "INSTRUMENT_AUTOINVEST"),
        # A plan that stopped long ago is gone, and a buy placed by hand is no plan.
        _scheduled(14, "IE00B4L5Y983", "EUNLd_EQ", "30", 300, "INSTRUMENT_AUTOINVEST"),
        _scheduled(15, "IE00B4L5Y983", "EUNLd_EQ", "30", 270, "INSTRUMENT_AUTOINVEST"),
        _order(16, CMG, "CMG_US_EQ", "BUY", "1", "60", "60", when=_ago(20)),
    ]]
    db = _memory_db()
    user = _setup(db)
    t212.sync(db, user.id)

    plans = {p["isin"]: p for p in t212.savings_plans(db, user.id)}
    assert set(plans) == {VHYL, CMG}
    vhyl = plans[VHYL]
    assert vhyl["frequency"] == "MONTHLY" and vhyl["amount"] == "50.00" and vhyl["runs"] == 3
    assert vhyl["pie"] == "Dividends" and "(pie Dividends)" in vhyl["name"]
    assert vhyl["next_execution_date"] == (datetime.now(UTC).date() - timedelta(days=5) + timedelta(days=30)).isoformat()
    assert plans[CMG]["frequency"] == "UNKNOWN" and plans[CMG]["next_execution_date"] is None

    # The monthly plan reads them like Scalable's: VHYL counts 50 € a month, CMG is listed but not counted.
    running = {p["isin"]: p for p in running_savings_plans(db, user.id, plan_settings(db))}
    assert running[VHYL]["broker"] == "trading212" and running[VHYL]["monthly_eur"] == 50.0
    assert running[VHYL]["inferred"] is True and running[VHYL]["running"] is True
    assert running[CMG]["monthly_eur"] is None


def test_a_hand_entered_holding_matching_a_trading212_position_can_be_reconciled(fake):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.foundation.models.entities import Holding
    from app.foundation.portfolio_service import main_portfolio
    from app.main import app

    db = _memory_db()
    user = _setup(db)
    manual = Holding(portfolio_id=main_portfolio(db, user.id).id, isin=CMG, name="Chipotle", asset_type="stock",
                     quantity=Decimal("10"), avg_buy_price=Decimal("50"), source="manual")
    db.add(manual)
    db.commit()
    t212.sync(db, user.id)
    assert trading212_status.status(db, user.id)["pending_reconciliation"] == 1
    from app.foundation import broker_status
    from app.foundation.control_center import attention_items

    # Counted at Trading 212, where the review lists it, and not at Scalable.
    assert broker_status.status(db, user.id)["pending_reconciliation"] == 0
    review = [i for i in attention_items(db) if i["id"] == "trading212-review"]
    assert review and review[0]["href"].endswith("#trading212-review")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        preview = client.get("/api/trading212/reconcile").json()
        assert [(r["isin"], r["source"], r["quantity_matches"]) for r in preview] == [(CMG, "trading212", True)]
        done = client.post("/api/trading212/reconcile", json={"decisions": {manual.id: "replace"}}).json()
    finally:
        app.dependency_overrides.clear()
    assert done == {"replaced": 1, "kept": 0}
    assert trading212_status.status(db, user.id)["pending_reconciliation"] == 0


def test_a_connected_broker_has_its_home_row_before_its_first_sync():
    from app.foundation.portfolio_service import wealth_summary

    db = _memory_db()
    user = _setup(db)  # key saved and switched on, nothing synced yet
    rows = {r["source"]: r for r in wealth_summary(db, user.id)["by_broker"]}
    assert rows["trading212"]["total"] == 0 and rows["trading212"]["last_synced"] is None
    assert "scalable" not in rows  # never set up
