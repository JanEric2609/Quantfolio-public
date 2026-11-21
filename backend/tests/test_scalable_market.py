"""The desk's typed market reads (ADR 0027), parsed from fixtures with fake IDs."""
import json
import subprocess
from datetime import date
from decimal import Decimal

import pytest

from app.foundation.scalable.cli import ScalableCli, ScalableCliConfig, ScalableProtocolError
from app.foundation.scalable import market

KO_ISIN = "DE000AAAAAA1"
ETF = "DE0005933931"


def _cli(response: dict, code: int = 0):
    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, code, json.dumps(response).encode(), b"")
    return ScalableCli(ScalableCliConfig(wrapper_path="/fake/quantfolio-sc-ro", cli_user="",
                                         max_attempts=1, trusted_wrapper_only=False), runner=runner)


def _ok(data):
    return {"ok": True, "command": "x", "data": data}


def test_quote_parses_decimals():
    cli = _cli(_ok({"result": {
        "isin": KO_ISIN, "name": "Fake KO", "security_type": "DERIVATIVE", "quote_bid_price": 1.23,
        "quote_ask_price": "1.25", "quote_mid_price": 1.24, "quote_currency": "EUR",
        "quote_is_outdated": False, "quote_timestamp_utc": "2026-11-20T10:00:00.123Z"}}))
    q = market.get_quote(cli, KO_ISIN)
    assert q and q.bid == Decimal("1.23") and q.ask == Decimal("1.25") and q.ts_utc is not None


def test_index_with_null_quote_is_none():
    cli = _cli(_ok({"result": {"isin": "DE0008469008", "security_type": "INDEX", "quote_bid_price": None,
                               "quote_ask_price": None}}))
    assert market.get_quote(cli, "DE0008469008") is None


def test_quote_without_isin_is_a_protocol_error():
    with pytest.raises(ScalableProtocolError):
        market.get_quote(_cli(_ok({"result": {"quote_bid_price": 1}})), KO_ISIN)


def test_listings_parse_expiry_and_open_end():
    cli = _cli(_ok({"result": {"total_available": 2, "count": 2, "items": [
        {"isin": KO_ISIN, "issuer": "VONTOBEL", "strategy": "long", "product_subcategory": "TURBO",
         "leverage": 4.8, "knockout_barrier": {"kind": "x", "value": 100}, "distance_to_knockout": 0.2,
         "strike": {"value": 99}, "expiry_date": {"epoch_day": 20800}, "expiry_is_open_end": False,
         "delta": None, "underlying_isin": "DE0008469008"},
        {"isin": "DE000AAAAAA2", "issuer": "X", "strategy": "long", "expiry_date": None,
         "expiry_is_open_end": True},
        {"no_isin": True},
    ]}}))
    rows = market.search_derivatives(cli, underlying="DE0008469008", product_type="knockout", strategy="long")
    assert len(rows) == 2
    assert rows[0].expiry == date(1970, 1, 1).fromordinal(date(1970, 1, 1).toordinal() + 20800)
    assert rows[0].leverage == Decimal("4.8") and rows[1].expiry is None


def test_chart_sorted_and_tolerates_wrapped_or_not():
    pts = [{"mid_price": 2, "timestamp_utc": "2026-11-20T09:30:00Z"},
           {"mid_price": 1, "timestamp_utc": "2026-11-20T09:00:00Z"}]
    for data in ({"data_points": pts}, {"result": {"data_points": pts}}):
        out = market.get_chart(_cli(_ok(data)), ETF, "7d")
        assert [p[1] for p in out] == [Decimal(1), Decimal(2)]


def test_chart_without_points_is_refused():
    with pytest.raises(ScalableProtocolError):
        market.get_chart(_cli(_ok({"x": 1})), ETF, "7d")


def test_news_parsed_defensively():
    cli = _cli(_ok({"result": {"items": [{"title": " Hello ", "id": 7}, {"nothing": 1}]}}))
    assert market.get_security_news(cli, ETF) == [{"id": "7", "title": "Hello", "published": ""}]
    assert market.get_security_news(_cli(_ok({"weird": True})), ETF) == []


def test_outdated_wrapper_maps_to_a_clear_state():
    err = {"ok": False, "command": "q", "error": {"code": "command_not_allowed", "message": "no"}, "hints": []}
    with pytest.raises(market.WrapperOutdated):
        market.get_quote(_cli(err, code=10), KO_ISIN)
