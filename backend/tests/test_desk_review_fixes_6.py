"""Regression tests for the sixth 2026-11-20 review of the derivatives desk (/code-review)."""
import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, quote_result, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import jobs, news, newswire, service
from app.decision.desk.ledger import append_event
from app.decision.desk.signals import index_momentum, owner_calls
from app.decision.desk.underlyings import ALIASES, resolve_underlying
from app.foundation import news_wire
from app.foundation.core.db import Base
from app.foundation.models.entities import ActivityLedgerEntry, DeskEvent, DeskPosition, IndexUniverseMember

KO = "DE000KO000A1"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)


# 1. A proposal a tap just filled is never overwritten with "expired".
def test_expiry_does_not_overwrite_a_tap_that_committed_first(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monitor, tap = make(), make()
    user = make_user(monitor)
    p = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                     isin=KO, underlying_isin=ALIASES["DAX"].isin, direction="long", status="proposed")
    monitor.add(p)
    monitor.flush()
    append_event(monitor, "proposal", service.MODE, {"proposed_at": (NOW - timedelta(seconds=181)).isoformat()},
                 p.id, now=NOW)
    monitor.commit()
    real = service._latest_proposal

    def tap_commits_meanwhile(db, pid):
        mine = tap.get(DeskPosition, pid)
        mine.status = "open"  # the tap that held the row lock commits its fill first
        tap.commit()
        return real(db, pid)

    monkeypatch.setattr(service, "_latest_proposal", tap_commits_meanwhile)
    assert service.expire_proposals(monitor, NOW) == 0
    monitor.commit()
    assert monitor.get(DeskPosition, p.id).status == "open"
    assert monitor.query(DeskEvent).filter(DeskEvent.kind == "expired").count() == 0


# 2. One failed price read no longer leaves a trade without its underlying move.
def test_a_failed_read_at_the_fill_falls_back_to_the_proposals_reference_price():
    db = _memory_db()
    user = make_user(db)
    item = ko_item(KO, lev=4, distance=0.25)
    sc = FakeSc(standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, {"knockout": [item]},
                                 chart_pts=daily_points()))
    res = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                          product_type="knockout", cli=sc.cli(), now=NOW)
    tap_at = NOW + timedelta(seconds=20)

    def no_etf(args):
        if "--isin" in args and DAX_ETF in args:
            return {"result": quote_result(DAX_ETF, 100, 100.1, now=tap_at, outdated=True)}
        return standard_handler({KO: ("0.995", "1.00")}, now=tap_at)(args)

    assert service.take(db, res.position.id, res.nonce, FakeSc(no_etf).cli(), tap_at, ALIASES["DAX"]).status == "filled"
    assert res.position.underlying_entry == Decimal("100.05")  # the proposal's read, not None


def test_an_exit_without_a_fresh_price_uses_the_last_chart_point():
    now = NOW + timedelta(hours=2)
    pts = [{"mid_price": 101.5, "timestamp_utc": (now - timedelta(minutes=10)).astimezone(UTC).isoformat()},
           {"mid_price": 90.0, "timestamp_utc": (now - timedelta(hours=2)).astimezone(UTC).isoformat()}]

    def handle(args):
        if args[:2] == ["broker", "quote"]:
            return {"result": quote_result(DAX_ETF, 100, 100.1, now=now - timedelta(minutes=5))}  # too old
        return {"data_points": pts}

    assert service.underlying_exit_level(None, FakeSc(handle).cli(), ALIASES["DAX"], now) == Decimal("101.5")


# 3. A German ticker does not borrow a US company's earnings date.
def test_a_xetra_ticker_keeps_its_own_symbol_for_the_earnings_guard():
    db = _memory_db()
    db.add(IndexUniverseMember(index_key="dax", isin="DE0008404005", name="Allianz SE", symbol="ALV.DE"))
    db.commit()
    u = resolve_underlying(db, "ALV")
    assert (u.isin, u.exchange, u.symbol) == ("DE0008404005", "XETR", "ALV.DE")


# 4. The owner's own round trip between two syncs is logged.
def test_a_derivative_bought_and_sold_between_syncs_is_logged(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    for i, (isin, side) in enumerate([(KO, "buy"), (KO, "sell"), ("IE00B4L5Y983", "buy"), ("DE000GONE001", "buy"), ("BAD", "buy")]):
        db.add(ActivityLedgerEntry(user_id=user.id, source="scalable", dedupe_hash=f"h{i}", activity_type=side,
                                   date=NOW.date(), description=isin, isin=isin, quantity=Decimal(10),
                                   price=Decimal("1.2")))
    db.commit()
    asked: list[str] = []

    def handle(args):
        isin = args[args.index("--isin") + 1]
        asked.append(isin)
        if isin == "DE000GONE001":
            return {"result": {"isin": isin, "quote_bid_price": None, "quote_ask_price": None}}
        q = quote_result(isin, 1, 1.01)
        q["security_type"] = "DERIVATIVE" if isin == KO else "ETF"
        return {"result": q}

    monkeypatch.setattr("app.foundation.scalable.service.make_cli", lambda db_: FakeSc(handle).cli())
    # The knock-out's buy and sell, not the ETF; a malformed ISIN (refused as input) does not stop the others.
    assert jobs.reconcile_external(db, NOW) == 2
    assert sorted(asked) == ["DE000GONE001", KO, "IE00B4L5Y983"]
    asked.clear()
    assert jobs.reconcile_external(db, NOW + timedelta(hours=1)) == 0 and asked == []  # cached; no quote: tomorrow
    jobs.reconcile_external(db, NOW + timedelta(days=1))
    assert asked == ["DE000GONE001"]


# 5. Smaller ones.
def test_the_us_rule_reads_bars_around_the_ticks_own_time(monkeypatch):
    seen: dict[str, datetime] = {}

    class Provider:
        def get_intraday_bars(self, symbol, timeframe, start, end, **kw):
            seen.update(start=start, end=end)
            return []

    monkeypatch.setattr(news, "alpaca_for", lambda db: Provider())
    plan = next(p for p in index_momentum.plans_for_day(NOW.date()) if p.key == "SPX")
    tick = plan.enter_at + timedelta(seconds=5)
    index_momentum.compute_r1(None, None, plan, tick)
    assert seen == {"start": tick - timedelta(days=7), "end": tick}


def test_two_horizons_get_their_own_message():
    with pytest.raises(owner_calls.IdeaParseError, match="one horizon"):
        owner_calls.parse_idea("/idea SAP up today 1d")


def test_a_news_alert_is_sent_off_the_streams_event_loop(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    threads: list[str] = []
    monkeypatch.setattr("app.decision.desk.telegram.alert",
                        lambda db_, uid, text, **kw: threads.append(threading.current_thread().name))

    async def rated(db_, item):
        return newswire.Rating(3, "down", "company", 1)

    monkeypatch.setattr(newswire, "rate", rated)
    item = news_wire.WireItem("alpaca", "9", "Nvidia warns", NOW.astimezone(UTC), symbols=("NVDA",))
    assert asyncio.run(newswire.ingest(db, user.id, item, {"NVDA": "holding:NVDA"}, now=NOW.astimezone(UTC))) == "sent"
    assert threads and threads[0] != threading.current_thread().name


def test_the_missing_move_rule_is_part_of_the_registered_spec():
    from app.decision.desk import prereg, scoring

    assert "missing_move" in prereg.SPEC["placebo"] and scoring.MAX_MISSING_MOVE == 0.10
    assert json.loads(json.dumps(prereg.SPEC))["placebo"]["missing_move"].startswith("trades without")
