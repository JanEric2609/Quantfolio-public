"""Regression tests for the ninth 2026-11-20 review of the derivatives desk (/code-review)."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import api_ops, cfd, jobs, news, newswire, service
from app.decision.desk import calendar as cal
from app.decision.desk.ledger import append_event
from app.decision.desk.underlyings import ALIASES
from app.foundation import news_wire
from app.foundation.core.db import Base
from app.foundation.models.entities import ActivityLedgerEntry, DeskPosition
from app.foundation.scalable import market

KO = "DE000KO000A1"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)


def _knocked_out_quote(args):
    """sc's answer for a knocked-out product: no bid or ask, but the security type."""
    isin = args[args.index("--isin") + 1]
    return {"result": {"isin": isin, "security_type": "DERIVATIVE", "quote_bid_price": None,
                       "quote_ask_price": None}}


# 1. A knocked-out product is still classified, so the owner's trade in it is logged.
def test_a_knocked_out_product_is_classified_without_a_bid(monkeypatch):
    assert market.get_quote(FakeSc(_knocked_out_quote).cli(), KO) is None
    assert market.get_security_type(FakeSc(_knocked_out_quote).cli(), KO) == "DERIVATIVE"
    db = _memory_db()
    user = make_user(db)
    db.add(ActivityLedgerEntry(user_id=user.id, source="scalable", dedupe_hash="h", activity_type="buy",
                               date=NOW.date(), description="KO", isin=KO, quantity=Decimal(10), price=Decimal("1")))
    db.commit()
    monkeypatch.setattr("app.foundation.scalable.service.make_cli", lambda db_: FakeSc(_knocked_out_quote).cli())
    assert jobs.reconcile_external(db, NOW) == 1


# 2. A 21:57 exit without a fresh bid closes at the day's last tick, not the next morning.
def _open_ko(db, user, exit_at):
    p = DeskPosition(user_id=user.id, cell="index_momentum:knockout", signal="index_momentum",
                     product_type="knockout", isin=KO, underlying_isin=ALIASES["SPX"].isin, direction="long",
                     status="open", units=1, entry_price=Decimal(1), entry_fee=Decimal("0.99"),
                     last_bid=Decimal("1.05"), entry_at=(exit_at - timedelta(minutes=27)).astimezone(UTC),
                     last_quote_at=(exit_at - timedelta(minutes=1)).astimezone(UTC), max_exit_at=exit_at.astimezone(UTC))
    db.add(p)
    db.flush()
    append_event(db, "fill", service.MODE, {}, p.id, now=exit_at)
    db.commit()
    return p


def test_a_late_exit_without_a_fresh_bid_closes_at_the_days_last_tick():
    db = _memory_db()
    user = make_user(db)
    exit_at = datetime(2026, 11, 18, 21, 57, tzinfo=cal.BERLIN)
    p = _open_ko(db, user, exit_at)
    t1 = exit_at + timedelta(minutes=1)  # 21:58: still waiting for a quote
    assert service.manage_open_positions(db, FakeSc(standard_handler({}, now=t1)).cli(), t1, {}) == []
    t2 = exit_at + timedelta(minutes=2)  # 21:59: the monitor's last tick of the day
    closed = service.manage_open_positions(db, FakeSc(standard_handler({}, now=t2)).cli(), t2, {})
    assert [r for _, r in closed] == ["horizon"] and p.exit_price == Decimal("1.05")


def test_a_cfd_halt_without_a_fresh_price_waits_for_one():
    db = _memory_db()
    user = make_user(db)
    sc = FakeSc(standard_handler({DAX_ETF: ("100", "100")}, chart_pts=daily_points(), now=NOW))
    p = cfd.open_signal(db, user.id, "DAX", ALIASES["DAX"], "long", NOW + timedelta(minutes=27), sc.cli(), NOW)
    db.commit()
    service.set_halt(db, True, "test", "test", now=NOW)
    t = NOW + timedelta(minutes=2)
    assert cfd.manage(db, FakeSc(standard_handler({}, now=t)).cli(), t, {}) == [] and p.status == "open"


# 3. A tap makes its network reads before it takes the desk's write lock.
def test_a_tap_reads_the_market_before_taking_the_write_lock(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    sc = FakeSc(standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                                 {"knockout": [ko_item(KO, lev=4, distance=0.25)]}, chart_pts=daily_points()))
    res = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                          product_type="knockout", cli=sc.cli(), now=NOW)
    db.commit()
    order: list[str] = []
    monkeypatch.setattr(service, "desk_write_lock", lambda db_: order.append("lock"))
    std = standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, now=NOW + timedelta(seconds=10))
    reading = FakeSc(lambda args: order.append("read") or std(args))
    tap = service.take(db, res.position.id, res.nonce, reading.cli(), NOW + timedelta(seconds=10), ALIASES["DAX"])
    assert tap.status == "filled" and order == ["read", "read", "lock"]
    assert res.position.underlying_entry == Decimal("100.05")


# 4. Open positions are always on the page, whatever the reconcile logged.
def test_open_positions_are_listed_however_many_external_trades_exist():
    db = _memory_db()
    user = make_user(db)
    db.add(DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                        isin=KO, underlying_isin=ALIASES["DAX"].isin, direction="long", status="open",
                        created_at=(NOW - timedelta(days=1)).astimezone(UTC)))
    for i in range(250):
        db.add(DeskPosition(user_id=user.id, cell="external", signal="external", product_type="unknown",
                            isin=f"DE000EXT{i:04d}", underlying_isin="", direction="long", status="closed",
                            exit_reason="external", created_at=NOW.astimezone(UTC)))
    db.commit()
    rows = api_ops.list_positions(db, user.id)
    assert [r["status"] for r in rows] == ["open"]
    assert len(api_ops.list_positions(db, user.id, status="closed")) == 200  # asked for: still listed


# 5. Only a tick with a working sc read clears the "wrapper outdated" flag.
def test_a_tick_without_sc_reads_keeps_the_wrapper_flag(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    db.add(DeskPosition(user_id=user.id, cell=cfd.CELL, signal="index_momentum", product_type="cfd",
                        isin=ALIASES["SPX"].isin, underlying_isin=ALIASES["SPX"].isin, direction="long",
                        status="open", entry_price=Decimal(600), quantity=Decimal(1), last_bid=Decimal(600),
                        entry_at=NOW.astimezone(UTC), last_quote_at=NOW.astimezone(UTC),
                        max_exit_at=(NOW + timedelta(hours=8)).astimezone(UTC)))
    db.commit()
    service.set_health(db, wrapper_outdated=True)
    monkeypatch.setattr(news, "alpaca_for", lambda db_: None)  # an SPX CFD is priced from Alpaca only
    sc_calls: list[list[str]] = []
    jobs.monitor_tick(db, NOW.replace(hour=8, minute=1), FakeSc(lambda args: sc_calls.append(args) or {}).cli())
    assert sc_calls == [] and service.health(db)["wrapper_outdated"] is True


# 6. A slow model does not stall the stream: items queue (or are dropped) while reading goes on.
def test_a_slow_rating_does_not_block_the_streams_receive_loop(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: db)
    monkeypatch.setattr(newswire, "_owner_and_key", lambda db_: ("u", ("k", "s")))
    monkeypatch.setattr(newswire, "watchlist", lambda db_, uid: {})
    monkeypatch.setattr(newswire, "_set_stream_health", lambda state, now=None: None)
    monkeypatch.setattr(newswire, "STREAM_QUEUE_MAX", 3)
    monkeypatch.setattr(newswire, "STREAM_DRAIN_S", 0.2)
    rated: list[str] = []

    async def slow_ingest(db_, uid, item, watch, *a, **k):
        await asyncio.sleep(0.05)
        rated.append(item.source_id)
        return "stored"

    monkeypatch.setattr(newswire, "ingest", slow_ingest)
    received: list[str] = []

    async def burst(key, secret, on_ready=None):
        on_ready()
        for i in range(50):
            received.append(str(i))
            yield news_wire.WireItem("alpaca", str(i), "h", NOW.astimezone(UTC))

    monkeypatch.setattr(news_wire, "alpaca_stream", burst)
    assert asyncio.run(newswire._stream_once()) == "closed"
    assert len(received) == 50 and 0 < len(rated) < 50  # all 50 read; the queue kept what fit


# Pullfrog on 1e706d5: the worker is never cancelled mid-item; the session closes only after it ends.
def test_the_item_in_hand_finishes_before_the_stream_closes_its_session(monkeypatch):
    events: list[str] = []

    class Db:
        def commit(self):
            pass

        def close(self):
            events.append("close")

    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: Db())
    monkeypatch.setattr(newswire, "_owner_and_key", lambda db_: ("u", ("k", "s")))
    monkeypatch.setattr(newswire, "watchlist", lambda db_, uid: {})
    monkeypatch.setattr(newswire, "_set_stream_health", lambda state, now=None: None)
    monkeypatch.setattr(newswire, "STREAM_DRAIN_S", 0.01)  # the drain gives up at once

    async def slow_ingest(db_, uid, item, watch, *a, **k):
        events.append(f"start {item.source_id}")
        await asyncio.sleep(0.1)  # e.g. the alert being sent in a thread on this session
        events.append(f"done {item.source_id}")
        return "sent"

    monkeypatch.setattr(newswire, "ingest", slow_ingest)

    async def two(key, secret, on_ready=None):
        on_ready()
        for i in range(2):
            yield news_wire.WireItem("alpaca", str(i), "h", NOW.astimezone(UTC))

    monkeypatch.setattr(news_wire, "alpaca_stream", two)
    assert asyncio.run(newswire._stream_once()) == "closed"
    assert events[:2] == ["start 0", "done 0"] and events[-1] == "close"
    assert "start 1" not in events  # the queued rest is dropped, never half-rated
