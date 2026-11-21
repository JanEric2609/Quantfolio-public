"""Regression tests for the fifth 2026-11-20 review of the derivatives desk (/code-review)."""
import asyncio
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from desk_helpers import NOW, make_user
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd, jobs, newswire, service
from app.decision.desk import calendar as cal
from app.decision.desk.limits import LIMITS
from app.decision.desk.underlyings import ALIASES
from app.foundation import control_center, news_wire
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent, DeskPosition


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# 1. A dead news stream stays visible: "since" moves only on a change, "connected" only after login.
def test_the_stream_state_keeps_its_start_while_the_same_failure_repeats():
    db = _memory_db()
    t0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    service.set_stream_health(db, "refused", t0)
    service.set_stream_health(db, "refused", t0 + timedelta(minutes=20))
    assert service.health(db)["news_stream_at"] == t0.isoformat()
    service.set_stream_health(db, "connected", t0 + timedelta(minutes=21))
    assert service.health(db)["news_stream_at"] == (t0 + timedelta(minutes=21)).isoformat()


def test_a_refused_login_never_reports_connected(monkeypatch):
    db = _memory_db()
    states: list[str] = []
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: db)
    monkeypatch.setattr(newswire, "_owner_and_key", lambda db_: ("u", ("k", "s")))
    monkeypatch.setattr(newswire, "watchlist", lambda db_, uid: {})
    monkeypatch.setattr(newswire, "_set_stream_health", lambda state, now=None: states.append(state))

    async def refused(key, secret, on_ready=None):
        raise news_wire.StreamRefused("auth refused [402]")
        yield  # pragma: no cover

    monkeypatch.setattr(news_wire, "alpaca_stream", refused)
    assert asyncio.run(newswire._stream_once()) == "refused" and states == []

    async def accepted(key, secret, on_ready=None):
        on_ready()
        return
        yield  # pragma: no cover

    monkeypatch.setattr(news_wire, "alpaca_stream", accepted)
    assert asyncio.run(newswire._stream_once()) == "closed" and states == ["connected"]


# 2. Alpaca's items carry tickers: a desk position on a US stock is watched by its ticker too.
def test_a_desk_stock_position_is_watched_by_its_ticker(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    db.add(DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                        isin="DE000KO000A1", underlying_isin="US67066G1040", underlying_label="NVIDIA",
                        direction="long", status="open"))
    db.commit()
    monkeypatch.setattr("app.foundation.live_positions.combined_positions", lambda db_, uid: [])
    monkeypatch.setattr("app.foundation.broker_instruments.us_ticker_for_isin",
                        lambda db_, isin: "NVDA" if isin == "US67066G1040" else None)
    watch = newswire.watchlist(db, user.id)
    assert watch["NVDA"] == "desk:NVIDIA" and watch["US67066G1040"] == "desk:NVIDIA"
    item = news_wire.WireItem("alpaca", "1", "NVIDIA cuts guidance", NOW.astimezone(UTC), symbols=("NVDA",))
    assert newswire.relevance(item, watch) == "desk:NVIDIA"


# 3. A message that cannot be built does not stop the others; a CFD's fallback still names side and market.
def test_a_failed_cfd_message_falls_back_to_one_that_still_says_buy(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p = DeskPosition(user_id=user.id, cell=cfd.CELL, signal="index_momentum", product_type="cfd", isin=ALIASES["SPX"].isin,
                     underlying_isin=ALIASES["SPX"].isin, underlying_label="S&P 500", direction="long", status="open",
                     product_name="USA 500 (USA500) CFD (Trading 212, manual)", entry_price=Decimal("600"),
                     quantity=Decimal("0.5"), entry_at=NOW.astimezone(UTC))
    db.add(p)
    db.commit()

    def broken(*a, **k):
        raise IntegrityError("insert", {}, Exception("duplicate bar"))

    monkeypatch.setattr(cfd, "open_message", broken)
    out = jobs._announce_fills(db, NOW, [p.id])
    assert len(out) == 1 and out[0].startswith("CFD signal (manual, Trading 212): BUY USA 500 (USA500)")


# 4. Two closes at once record one exit.
def test_a_position_closed_elsewhere_is_not_closed_again():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    a, b = make(), make()
    user = make_user(a)
    p = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                     isin="DE000KO000A1", underlying_isin=ALIASES["DAX"].isin, direction="long", status="open",
                     units=10, entry_price=Decimal("1"), entry_fee=Decimal("0.99"), entry_at=NOW.astimezone(UTC))
    a.add(p)
    a.commit()
    pa, pb = a.get(DeskPosition, p.id), b.get(DeskPosition, p.id)  # both sessions see it open
    assert service.close_position(a, pa, Decimal("1.1"), "stop", NOW) is True
    a.commit()
    assert service.close_position(b, pb, Decimal("0.9"), "owner_close", NOW) is False
    b.commit()
    assert pb.status == "closed" and pb.exit_reason == "stop"
    assert a.query(DeskEvent).filter(DeskEvent.kind == "exit").count() == 1
    q = DeskPosition(user_id=user.id, cell=cfd.CELL, signal="index_momentum", product_type="cfd",
                     isin=ALIASES["DAX"].isin, underlying_isin=ALIASES["DAX"].isin, direction="long", status="open",
                     entry_price=Decimal("100"), quantity=Decimal("1"), spread_pct=Decimal("0.0002"))
    a.add(q)
    a.commit()
    qb = b.get(DeskPosition, q.id)
    assert cfd.close(a, a.get(DeskPosition, q.id), Decimal("101"), "time", NOW) is True
    a.commit()
    assert cfd.close(b, qb, Decimal("99"), "owner_close", NOW) is False


# 5. The Control Center's stale-quote item uses the desk's own window start.
def test_the_stale_quote_item_does_not_fire_before_the_mornings_first_quotes():
    assert control_center.DESK_WINDOW_START == time(LIMITS.window_start_hour, LIMITS.window_start_minute)
    db = _memory_db()
    user = make_user(db)
    last_night = datetime(2026, 11, 17, 21, 59, tzinfo=cal.BERLIN).astimezone(UTC)
    db.add(DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                        isin="DE000KO000A1", underlying_isin=ALIASES["DAX"].isin, direction="long", status="open",
                        last_quote_at=last_night))
    db.commit()

    def ids(at):
        items: list = []
        control_center._desk_items(db, at.astimezone(UTC), items)
        return {i.id for i in items}

    assert "desk-quote-lost" not in ids(datetime(2026, 11, 18, 8, 10, tzinfo=cal.BERLIN))
    assert "desk-quote-lost" in ids(datetime(2026, 11, 18, 8, 16, tzinfo=cal.BERLIN))


# 6. Retention runs once a day from 03:00 UTC, whichever tick comes first.
@pytest.mark.parametrize("minute", [0, 7])
def test_prune_runs_once_a_day_even_when_the_0300_tick_is_late(minute):
    db = _memory_db()
    day = datetime(2026, 11, 21, tzinfo=UTC)
    assert newswire._prune_due(db, day.replace(hour=2, minute=59)) is False
    late = day.replace(hour=3, minute=minute)
    assert newswire._prune_due(db, late) is True
    newswire.prune(db, late)
    assert newswire._prune_due(db, late + timedelta(hours=5)) is False
    assert newswire._prune_due(db, late + timedelta(days=1)) is True
