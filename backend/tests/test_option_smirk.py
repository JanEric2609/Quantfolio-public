from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.providers import alpaca_provider as ap
from app.lab.forward_lab import option_smirk as om

TODAY = date(2026, 11, 24)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _row(kind, delta, iv, days=40):
    from datetime import timedelta

    return {"symbol": "X", "type": kind, "strike": 100.0, "expiration": TODAY + timedelta(days=days), "iv": iv,
            "delta": delta}


def test_parse_occ_and_snapshot_fields():
    row = ap._parse_occ("AAPL261218P00250000", {"impliedVolatility": 0.3, "greeks": {"delta": -0.27}})
    assert row == {"symbol": "AAPL261218P00250000", "type": "put", "strike": 250.0,
                   "expiration": date(2026, 12, 18), "iv": 0.3, "delta": -0.27}
    assert ap._parse_occ("garbage", {}) is None
    assert ap._parse_occ("AAPL261218P00250000", {"greeks": {}}) is None


def test_chain_parsing_and_pagination():
    prov = ap.AlpacaProvider("k", api_secret="s")
    pages = [
        {"snapshots": {"AAPL261218C00250000": {"impliedVolatility": 0.25, "greeks": {"delta": 0.5}}},
         "next_page_token": "t"},
        {"snapshots": {"AAPL261218P00200000": {"impliedVolatility": 0.35, "greeks": {"delta": -0.25}}}},
    ]
    with patch.object(prov, "_get", side_effect=pages) as get:
        out = prov.option_chain("AAPL", date(2026, 12, 1), date(2026, 12, 31))
    assert [r["type"] for r in out] == ["call", "put"] and get.call_count == 2


def test_403_is_data_unavailable():
    prov = ap.AlpacaProvider("k", api_secret="s")
    with patch.object(prov, "_get", side_effect=ValueError("Alpaca HTTP 403 for /AAPL")):
        with pytest.raises(ap.DataUnavailable, match="options_not_entitled"):
            prov.option_chain("AAPL", date(2026, 12, 1), date(2026, 12, 31))


def test_smirk_score_on_a_synthetic_chain():
    steep = [_row("put", -0.25, 0.40), _row("call", 0.5, 0.25), _row("put", -0.1, 0.5)]
    flat = [_row("put", -0.25, 0.26), _row("call", 0.5, 0.25)]
    assert om.smirk_score(steep, TODAY) == pytest.approx(-0.15)
    assert om.smirk_score(flat, TODAY) == pytest.approx(-0.01)
    assert om.smirk_score(flat, TODAY) > om.smirk_score(steep, TODAY)
    assert om.smirk_score([_row("put", -0.25, 0.3, days=5), _row("call", 0.5, 0.2, days=5)], TODAY) is None
    assert om.smirk_score([_row("call", 0.5, 0.2)], TODAY) is None
    assert om.smirk_score([_row("put", -0.9, 0.3), _row("call", 0.5, 0.2)], TODAY) is None  # no put near 0.25


def test_token_bucket_caps_the_rate():
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    bucket = om.TokenBucket(150, clock=lambda: clock["t"], sleep=sleep)
    for _ in range(150):
        bucket.wait()
    assert slept == []  # the first minute's tokens are free
    for _ in range(10):
        bucket.wait()
    assert len(slept) == 10 and all(abs(s - 0.4) < 1e-6 for s in slept)  # 60 s / 150 = 0.4 s each


def test_collect_stores_scores_and_reads_back_without_lookahead():
    db = _memory_db()
    chain = [_row("put", -0.25, 0.3), _row("call", 0.5, 0.25)]

    class P:
        def option_chain(self, sym, a, b, before_request=None):
            if sym == "NONE":
                return []
            return chain

    now = datetime(2026, 11, 24, 4, 30, tzinfo=UTC)
    out = om.collect(db, P(), ["AAA", "NONE", "BBB"], now, bucket=om.TokenBucket(10_000))
    assert out == {"names": 2, "asked": 3, "stopped": None}
    assert set(om.stored_scores(db, "2026-11-23")) == {"AAA", "BBB"}
    assert om.stored_scores(db, "2026-11-24") == {}  # collected the same day: not after the scoring day
    assert om.stored_scores(db, "2026-11-10") == {}  # too old a snapshot for that day


def test_collect_stops_when_not_entitled():
    db = _memory_db()

    class P:
        def option_chain(self, sym, a, b, before_request=None):
            raise ap.DataUnavailable("options_not_entitled")

    out = om.collect(db, P(), ["A", "B"], datetime(2026, 11, 24, 4, 30, tzinfo=UTC), bucket=om.TokenBucket(10_000))
    assert out["stopped"] == "options_not_entitled" and out["names"] == 0


def test_every_chain_page_counts_against_the_bucket():
    prov = ap.AlpacaProvider("k", api_secret="s")
    pages = [{"snapshots": {}, "next_page_token": "p2"}, {"snapshots": {}, "next_page_token": "p3"},
             {"snapshots": {}, "next_page_token": "p4"}]
    waits = []
    with patch.object(prov, "_get", side_effect=pages) as get:
        prov.option_chain("AAPL", date(2026, 12, 1), date(2026, 12, 31), before_request=lambda: waits.append(1))
    assert get.call_count == 3 and len(waits) == 3  # one token per page, and at most three pages


def test_collect_stops_on_rate_limit_and_keeps_what_it_read():
    db = _memory_db()
    chain = [_row("put", -0.25, 0.3), _row("call", 0.5, 0.25)]

    class P:
        def option_chain(self, sym, a, b, before_request=None):
            if before_request is not None:
                before_request()
            if sym == "LATE":
                raise ValueError("Alpaca HTTP 429 for /LATE")
            return chain

    now = datetime(2026, 11, 24, 4, 30, tzinfo=UTC)
    out = om.collect(db, P(), ["AAA", "LATE", "BBB"], now, bucket=om.TokenBucket(10_000))
    assert out == {"names": 1, "asked": 3, "stopped": "rate_limited"}
    assert set(om.stored_scores(db, "2026-11-23")) == {"AAA"}
