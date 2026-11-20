"""Regression tests for the 2026-11-20 review of the derivatives desk (one per finding)."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.decision.desk import api_ops, jobs, news, newswire, service
from app.decision.desk import calendar as cal
from app.decision.desk.guard import check_exit
from app.decision.desk.ledger import append_event
from app.decision.desk.signals import index_momentum, owner_calls
from app.decision.desk.underlyings import ALIASES
from app.foundation import news_wire
from app.foundation.models.entities import DeskEvent, DeskPosition, NewsWireItem
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, memory_db, standard_handler


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)
    monkeypatch.setattr(service, "underlying_price", lambda *a, **k: Decimal("100"))


def _idea(db, user, sc):
    return owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW)


def _setup(db):
    user = make_user(db)
    sc = FakeSc(standard_handler({"DE000KO000A1": ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                                 {"knockout": [ko_item("DE000KO000A1", lev=4, distance=0.25)]}, chart_pts=daily_points()))
    return user, sc


# 1. A failed tick sends nothing; the exit alert goes out once, after the commit that closes the position.
def test_a_failed_tick_sends_no_exit_alert_and_the_next_tick_sends_it_once(monkeypatch):
    db = memory_db()
    user, sc = _setup(db)
    res = _idea(db, user, sc).proposals[0]
    service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=10))
    db.commit()
    sent: list[str] = []
    monkeypatch.setattr("app.decision.desk.telegram.alert", lambda db, uid, text: sent.append(text))
    tick = NOW + timedelta(minutes=1)
    crash = FakeSc(standard_handler({"DE000KO000A1": ("0.80", "0.81"), DAX_ETF: ("95", "95.1")}, now=tick))

    def timeout(*a, **k):
        raise httpx.ConnectTimeout("alpaca")

    monkeypatch.setattr("app.decision.desk.cfd.manage", timeout)
    with pytest.raises(httpx.ConnectTimeout):
        jobs.monitor_tick(db, tick, crash.cli())
    db.rollback()  # what the job runner does with a failed tick
    assert sent == [] and db.get(DeskPosition, res.position.id).status == "open"
    monkeypatch.setattr("app.decision.desk.cfd.manage", lambda *a, **k: [])

    def crash_at(t):
        return FakeSc(standard_handler({"DE000KO000A1": ("0.80", "0.81"), DAX_ETF: ("95", "95.1")}, now=t)).cli()

    jobs.monitor_tick(db, tick + timedelta(minutes=1), crash_at(tick + timedelta(minutes=1)))
    assert len(sent) == 1 and sent[0].startswith("Desk paper exit (stop)")
    jobs.monitor_tick(db, tick + timedelta(minutes=2), crash_at(tick + timedelta(minutes=2)))
    assert len(sent) == 1


def test_a_network_error_reading_headlines_or_bars_is_caught(monkeypatch):
    monkeypatch.undo()

    class Down:
        def get_news(self, *a, **k):
            raise httpx.ReadTimeout("slow")

        def get_intraday_bars(self, *a, **k):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(news, "alpaca_for", lambda db: Down())
    spx = ALIASES["SPX"]
    assert news.headlines(None, None, spx, NOW) == []
    plan = next(p for p in index_momentum.plans_for_day(NOW.date()) if p.key == "SPX")
    with pytest.raises(index_momentum.DataUnavailable):
        index_momentum.compute_r1(None, None, plan)


def test_a_failed_price_read_retries_inside_the_window_then_logs_no_data(monkeypatch):
    db = memory_db()
    user = make_user(db)

    def down(db, cli, plan):
        raise index_momentum.DataUnavailable("ReadTimeout")

    monkeypatch.setattr(index_momentum, "compute_r1", down)
    plan = next(p for p in index_momentum.plans_for_day(NOW.date()) if p.key == "DAX")
    index_momentum.fire_due_signals(db, user.id, None, plan.enter_at + timedelta(seconds=5))
    assert db.query(DeskEvent).filter(DeskEvent.kind == "signal").count() == 0  # retried next minute
    index_momentum.fire_due_signals(db, user.id, None, plan.enter_at + index_momentum.FIRE_WINDOW - timedelta(seconds=30))
    evs = [e.payload for e in db.query(DeskEvent).filter(DeskEvent.kind == "signal") if '"DAX:' in e.payload]
    assert len(evs) == 1 and '"no_data"' in evs[0]


# 2. One unreachable feed does not stop the others.
def test_a_feed_timeout_is_a_value_error_so_the_other_feeds_still_run(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectTimeout("handelsblatt")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(ValueError, match="network: ConnectTimeout"):
        news_wire.fetch("https://feed.example", "ua")


# 3. "No fresh quote" can be retried with the same button.
def test_a_tap_without_a_fresh_quote_can_be_retried_with_the_same_code():
    db = memory_db()
    user, sc = _setup(db)
    res = _idea(db, user, sc).proposals[0]
    no_quote = FakeSc(standard_handler({DAX_ETF: ("100", "100.1")}))
    assert service.take(db, res.position.id, res.nonce, no_quote.cli(), NOW + timedelta(seconds=20)).status == "no_quote"
    again = service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=30))
    assert again.status == "filled" and res.position.status == "open"
    assert service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=40)).status == "replay"


# 4. The event log shows the newest events.
def test_latest_events_are_the_newest_in_ledger_order():
    db = memory_db()
    for i in range(5):
        append_event(db, "error", service.MODE, {"code": f"e{i}"}, now=NOW + timedelta(seconds=i))
    db.commit()
    rows = api_ops.list_events(db, limit=2, latest=True)
    assert [r["payload"]["code"] for r in rows] == ["e3", "e4"]
    assert [r["payload"]["code"] for r in api_ops.list_events(db, limit=2)] == ["e0", "e1"]


# 6. A re-ask that leaves no unit refuses the fill.
def test_a_reask_to_zero_units_is_refused_not_filled():
    db = memory_db()
    user, sc = _setup(db)
    res = _idea(db, user, sc).proposals[0]
    jump = FakeSc(standard_handler({"DE000KO000A1": ("99", "100"), DAX_ETF: ("100", "100.1")}))
    tap = service.take(db, res.position.id, res.nonce, jump.cli(), NOW + timedelta(seconds=30))
    assert tap.status == "refused" and res.position.status == "rejected" and res.position.entry_at is None
    assert db.query(DeskEvent).filter(DeskEvent.kind == "fill").count() == 0


# 7. The US rule starts from the regular close, as registered and replayed.
def test_r1_us_ignores_after_hours_bars():
    def bar(day, hh, mm, c):
        return {"t": datetime(2026, 11, day, hh, mm, tzinfo=cal.NEW_YORK).astimezone(UTC), "c": Decimal(c)}

    bars = [bar(17, 15, 0, "99"), bar(17, 15, 30, "100"), bar(17, 19, 30, "90"),
            bar(18, 4, 0, "95"), bar(18, 9, 30, "101")]
    assert index_momentum.compute_r1_us(bars, datetime(2026, 11, 18).date()) == Decimal("0.01")


# 8. Times read back from SQLite carry no zone.
def test_check_exit_accepts_naive_stored_times():
    past = (NOW - timedelta(hours=1)).astimezone(UTC).replace(tzinfo=None)
    pos = SimpleNamespace(stop_price=None, max_exit_at=past, last_quote_at=None, entry_at=None)
    assert check_exit(pos, Decimal("1"), NOW) == "horizon"
    pos = SimpleNamespace(stop_price=None, max_exit_at=None, last_quote_at=past, entry_at=None)
    assert check_exit(pos, None, NOW) == "quote_lost"


# 9. Old sc headlines are not desk news.
def test_old_scalable_headlines_are_dropped(monkeypatch):
    monkeypatch.undo()
    old = (NOW - timedelta(days=20)).astimezone(UTC).isoformat()
    new = (NOW - timedelta(minutes=10)).astimezone(UTC).isoformat()
    monkeypatch.setattr("app.foundation.scalable.market.get_security_news", lambda cli, isin: [
        {"id": "a", "title": "old", "published": old}, {"id": "b", "title": "new", "published": new},
        {"id": "c", "title": "undated", "published": ""}])
    got = news.headlines(None, None, ALIASES["DAX"], NOW - timedelta(hours=6))
    assert [h["id"] for h in got] == ["b", "c"]


# 10. One message per underlying, logged on every position.
def test_desk_news_is_sent_once_per_underlying(monkeypatch):
    db = memory_db()
    user = make_user(db)
    dax = ALIASES["DAX"]
    for kind in ("knockout", "warrant", "cfd"):
        db.add(DeskPosition(user_id=user.id, signal="index_momentum", cell=f"index_momentum:{kind}", product_type=kind,
                            direction="long", isin=f"DE000{kind[:2].upper()}00001", underlying_isin=dax.isin,
                            underlying_label="DAX", status="open", liquid=True))
    db.commit()
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [{"id": "n1", "title": "DAX falls"}])
    outbox: list[str] = []
    assert jobs._news_check(db, None, NOW, {dax.isin: dax}, outbox) == 1
    assert outbox == ["Desk news on DAX (logged only): DAX falls"]
    assert db.query(DeskEvent).filter(DeskEvent.kind == "news_seen").count() == 3
    assert jobs._news_check(db, None, NOW, {dax.isin: dax}, outbox) == 0 and len(outbox) == 1


# The news wire stores "sent" before it sends.
def test_the_news_wire_commits_before_it_sends(monkeypatch):
    db = memory_db()
    user = make_user(db)
    states: list[str | None] = []

    def alert(db_, uid, text):
        states.append(db.get(NewsWireItem, "alpaca:9").alert)

    monkeypatch.setattr("app.decision.desk.telegram.alert", alert)

    async def rated(db_, item):
        return newswire.Rating(3, "down", "company", 1)

    monkeypatch.setattr(newswire, "rate", rated)
    item = news_wire.WireItem("alpaca", "9", "Nvidia warns", NOW.astimezone(UTC), symbols=("NVDA",))
    assert asyncio.run(newswire.ingest(db, user.id, item, {"NVDA": "holding:NVDA"}, now=NOW.astimezone(UTC))) == "sent"
    assert states == ["sent"]
