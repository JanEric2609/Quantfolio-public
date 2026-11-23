"""Regression tests for the fourth 2026-11-20 review of the derivatives desk (/code-review and pullfrog)."""
import json
import subprocess
from datetime import UTC, date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import jobs, news, service, telegram
from app.decision.desk import calendar as cal
from app.decision.desk.signals import index_momentum, owner_calls
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent, DeskPosition
from app.foundation.scalable.cli import ScalableCli, ScalableCliConfig, ScalableError


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


KO = "DE000KO000A1"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)


def _refusing_cli(code):
    def run(argv, **kw):
        out = {"ok": False, "command": "q", "error": {"code": code, "message": "no"}, "hints": []}
        return subprocess.CompletedProcess(argv, 10 if code == "command_not_allowed" else 20, json.dumps(out).encode(), b"")

    return ScalableCli(ScalableCliConfig(wrapper_path="/fake/quantfolio-sc-ro", cli_user="", max_attempts=1,
                                         trusted_wrapper_only=False), runner=run)


def _open_ko(db, user, last_quote_minutes=15):
    p = DeskPosition(user_id=user.id, mode=service.MODE, signal="owner_call", cell="owner_call:knockout",
                     product_type="knockout", direction="long", isin=KO, underlying_isin=ALIASES["DAX"].isin,
                     underlying_label="DAX", status="open", liquid=True, units=10, entry_price=Decimal("1"),
                     entry_fee=Decimal("0.99"), stop_price=Decimal("0.5"), last_bid=Decimal("0.9"),
                     entry_at=(NOW - timedelta(hours=1)).astimezone(UTC),
                     last_quote_at=(NOW - timedelta(minutes=last_quote_minutes)).astimezone(UTC),
                     max_exit_at=(NOW + timedelta(hours=8)).astimezone(UTC))
    db.add(p)
    db.commit()
    return p


# 1. An outdated wrapper or an expired session is reported, never hidden behind quote_lost.
def test_an_outdated_wrapper_fails_the_tick_instead_of_closing_positions_as_quote_lost():
    db = _memory_db()
    user = make_user(db)
    p = _open_ko(db, user)
    result = jobs.monitor_tick(db, NOW, _refusing_cli("command_not_allowed"))
    assert result == {"skipped": "wrapper_outdated"} and service.health(db)["wrapper_outdated"] is True
    assert db.get(DeskPosition, p.id).status == "open"  # not judged on reads that were never allowed


def test_an_idle_tick_does_not_clear_the_outdated_wrapper_flag():
    db = _memory_db()
    make_user(db)
    service.set_health(db, wrapper_outdated=True)
    jobs.monitor_tick(db, NOW.replace(hour=8, minute=1), FakeSc(standard_handler({})).cli())  # 08:01: no position, no rule, before the heads-up window
    assert service.health(db)["wrapper_outdated"] is True


def test_an_expired_session_in_the_exit_checks_shows_as_the_desks_last_error():
    db = _memory_db()
    user = make_user(db)
    _open_ko(db, user, last_quote_minutes=1)
    jobs.monitor_tick(db, NOW.replace(minute=1), _refusing_cli("session_expired"))
    assert service.health(db)["last_error"] == "session_expired"
    errors = [json.loads(e.payload) for e in db.query(DeskEvent).filter(DeskEvent.kind == "error")]
    assert errors == [{"code": "session_expired"}]


# 2. The knock-out barrier is anchored in the same snapshot as the listing's distance.
def _ko_listing(distance=0.25):
    item = ko_item(KO, lev=4, distance=distance)
    item["knockout_barrier"] = {"kind": "x", "value": 19000}
    return item


def test_the_barrier_is_anchored_at_the_proposal_not_at_a_later_fill():
    db = _memory_db()
    user = make_user(db)
    at_proposal = FakeSc(standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                                          {"knockout": [_ko_listing()]}, chart_pts=daily_points()))
    res = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                          product_type="knockout", cli=at_proposal.cli(), now=NOW)
    proposal = json.loads(db.query(DeskEvent).filter(DeskEvent.kind == "proposal").one().payload)
    assert Decimal(proposal["barrier_ref"]) == Decimal("75.037500")  # 100.05 x (1 - 0.25)
    # The ETF rallied 10 % before the tap: the barrier stays where it was proposed, not 10 % higher.
    tap_at = NOW + timedelta(seconds=40)
    rallied = FakeSc(standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("110", "110.1")}, now=tap_at))
    assert service.take(db, res.position.id, res.nonce, rallied.cli(), tap_at, ALIASES["DAX"]).status == "filled"
    fill = json.loads(db.query(DeskEvent).filter(DeskEvent.kind == "fill").one().payload)
    assert fill["barrier_ref"] == proposal["barrier_ref"] and Decimal(fill["underlying_entry"]) == Decimal("110.05")


def test_a_knockout_whose_reference_price_cannot_be_read_is_not_proposed():
    db = _memory_db()
    user = make_user(db)
    no_etf = FakeSc(standard_handler({KO: ("0.995", "1.00")}, {"knockout": [_ko_listing()]}, chart_pts=daily_points()))
    monkey_sigma = service.underlying_sigma
    try:
        service.underlying_sigma = lambda *a, **k: Decimal("0.01")
        res = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                              product_type="knockout", cli=no_etf.cli(), now=NOW)
    finally:
        service.underlying_sigma = monkey_sigma
    assert res.position is None and res.reason == service.NO_REFERENCE
    assert "barrier could not be watched" in owner_calls.format_proposal(res, ALIASES["DAX"], "knockout")


# 3. The index rule measures quote ages and fills on the wall clock, not the tick's start.
def test_the_index_rule_measures_quote_age_when_the_quote_is_read():
    db = _memory_db()
    user = make_user(db)
    enter = index_momentum.plans_for_day(date(2026, 11, 18))[0].enter_at  # DAX, 17:00 Berlin
    at = enter + timedelta(seconds=5)
    day_pts = [{"mid_price": 100, "timestamp_utc": "2026-11-17T16:30:00Z"},
               {"mid_price": 101, "timestamp_utc": "2026-11-18T08:30:00Z"}]
    std = standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                           {"knockout": [ko_item(KO, lev=4, distance=0.25)]}, now=at)

    def handler(args):
        if args[:2] == ["broker", "chart"] and args[args.index("--timeframe") + 1] == "7d":
            return {"data_points": day_pts}
        return std(args)

    # Earlier reads in the tick took 70 s: quotes stamped 10 s before the tick are 80 s old now.
    late = at + timedelta(seconds=70)
    index_momentum.fire_due_signals(db, user.id, FakeSc(handler).cli(), at, clock=lambda: late)
    assert db.query(DeskPosition).filter(DeskPosition.product_type == "knockout",
                                         DeskPosition.status == "open").count() == 0
    # 20 s later instead: fresh, and the fill is stamped when it happened.
    db2 = _memory_db()
    user2 = make_user(db2)
    soon = at + timedelta(seconds=20)
    index_momentum.fire_due_signals(db2, user2.id, FakeSc(handler).cli(), at, clock=lambda: soon)
    ko = db2.query(DeskPosition).filter(DeskPosition.product_type == "knockout").one()
    assert ko.status == "open" and service._aware(ko.entry_at) == soon.astimezone(UTC)


# 4. A failed idea leaves nothing behind; another account's button does nothing.
def test_a_failed_idea_is_rolled_back(monkeypatch):
    db = _memory_db()
    user = make_user(db)

    def half_built(db_, uid, idea, cli, now, clock=None):
        db_.add(DeskPosition(user_id=uid, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                             isin=KO, underlying_isin=ALIASES["DAX"].isin, direction="long", status="proposed"))
        db_.flush()
        raise ScalableError("session_busy", "lock held")

    monkeypatch.setattr(owner_calls, "start_idea", half_built)
    monkeypatch.setattr("app.foundation.scalable.service.make_cli", lambda db_: None)
    said: list[str] = []
    monkeypatch.setattr(telegram, "_say", lambda db_, chat, text, markup=None: said.append(text))
    telegram.handle_idea(db, SimpleNamespace(user_id=user.id), "1", "/idea DAX up")
    db.commit()  # the next update's commit must not store the half-built proposal
    assert said == ["Could not read the market: session_busy."] and db.query(DeskPosition).count() == 0


def test_a_take_button_of_another_account_is_unknown(monkeypatch):
    db = _memory_db()
    owner = make_user(db)
    other = make_user(db, role="admin", name="other")
    pos = DeskPosition(user_id=owner.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                       isin=KO, underlying_isin=ALIASES["DAX"].isin, direction="long", status="proposed")
    db.add(pos)
    db.commit()
    monkeypatch.setattr(service, "take", lambda *a, **k: pytest.fail("must not take another account's proposal"))
    assert telegram.handle_take(db, SimpleNamespace(user_id=other.id), "1", f"desk:take:{pos.id}:n") == "Unknown proposal."


# 5. Smaller ones.
def test_2027_fomc_follows_the_revised_schedule():
    assert ("fomc", "2027-11-17") in cal.macro_events_between(date(2027, 11, 17), date(2027, 11, 17))
    assert not [e for e in cal.macro_events_between(date(2027, 10, 27), date(2027, 10, 27)) if e[0] == "fomc"]
    assert not cal.macro_events_between(date(2027, 12, 8), date(2027, 12, 8))


def test_the_api_re_registers_the_telegram_webhook_at_startup(monkeypatch):
    from app import main
    from app.foundation import telegram_updates

    db = _memory_db()
    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    calls: list[str] = []
    monkeypatch.setattr(telegram_updates, "telegram_mode", lambda db_: "webhook")
    monkeypatch.setattr(telegram_updates, "sync_webhook", lambda db_: calls.append("sync") or True)
    main._resync_telegram_webhook()
    assert calls == ["sync"]
    monkeypatch.setattr(telegram_updates, "telegram_mode", lambda db_: "polling")
    main._resync_telegram_webhook()
    assert calls == ["sync"]
