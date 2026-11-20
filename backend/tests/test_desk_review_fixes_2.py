"""Regression tests for the second 2026-11-20 review of the derivatives desk (one per finding)."""
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import jobs, news, newswire, service, telegram
from app.decision.desk.signals import owner_calls
from app.decision.desk.underlyings import Underlying
from app.foundation import telegram_updates
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent, DeskPosition, NewsWireItem, User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


KO = "DE000KO000A1"
US_STOCK = Underlying("US0000000001", "Fake Corp", "XNYS", True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)
    monkeypatch.setattr(service, "underlying_price", lambda *a, **k: Decimal("100"))


def _sc(now=NOW, quotes=None):
    q = {KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")} if quotes is None else quotes
    return FakeSc(standard_handler(q, {"knockout": [ko_item(KO, lev=4, distance=0.25)]},
                                   chart_pts=daily_points(now=now), now=now))


def _propose_us(db, user, now):
    return service.propose(db, user.id, signal="owner_call", underlying=US_STOCK, direction="long",
                           product_type="knockout", cli=_sc(now).cli(), now=now, horizon_days=1)


def _open_ko(db, user, *, barrier="90", direction="long"):
    p = DeskPosition(user_id=user.id, mode=service.MODE, signal="owner_call", cell="owner_call:knockout",
                     product_type="knockout", direction=direction, isin=KO, underlying_isin=US_STOCK.isin,
                     underlying_label="Fake Corp", status="open", liquid=True, units=10, entry_price=Decimal("1.00"),
                     entry_fee=Decimal("0.99"), stop_price=Decimal("0.50"), last_bid=Decimal("0.90"),
                     barrier_or_strike=Decimal(barrier), entry_at=(NOW - timedelta(minutes=5)).astimezone(UTC),
                     last_quote_at=(NOW - timedelta(minutes=1)).astimezone(UTC),
                     max_exit_at=(NOW + timedelta(hours=8)).astimezone(UTC))
    db.add(p)
    db.commit()
    return p


# 1. "today" after 21:30 Berlin is not proposed, and a tap past the latest exit is not filled.
def test_a_today_idea_after_the_cutoff_is_not_proposed():
    db = _memory_db()
    user = make_user(db)
    late = NOW.replace(hour=21, minute=35)
    res = _propose_us(db, user, late)
    assert res.position is None and res.reason == service.EXIT_PASSED
    assert "exit time passed" in owner_calls.format_proposal(res, US_STOCK, "knockout")


def test_a_tap_after_the_latest_exit_is_refused():
    db = _memory_db()
    user = make_user(db)
    at = NOW.replace(hour=21, minute=29)
    res = _propose_us(db, user, at)
    assert res.nonce and res.position.max_exit_at is not None
    tap_at = NOW.replace(hour=21, minute=30, second=30)
    tap = service.take(db, res.position.id, res.nonce, _sc(tap_at).cli(), tap_at, US_STOCK)
    assert tap.status == "refused" and service.EXIT_PASSED in tap.message
    assert res.position.status == "rejected" and res.position.entry_at is None


# 2. Paper is sized against the whole pot, not what live would have left.
def test_paper_sizing_ignores_the_pot_already_used():
    db = _memory_db()
    user = make_user(db)
    first = _propose_us(db, user, NOW.replace(hour=16)).position
    db.add(DeskPosition(user_id=user.id, mode=service.MODE, signal="owner_call", cell="owner_call:knockout",
                        product_type="knockout", direction="long", isin="DE000KO000B2", underlying_isin=DAX_ETF,
                        underlying_label="DAX", status="open", liquid=True, units=240, entry_price=Decimal("1"),
                        would_take_live=True, entry_at=NOW.astimezone(UTC)))
    db.commit()
    second = _propose_us(db, user, NOW.replace(hour=16, minute=1)).position
    assert first.units and second.units == first.units and second.status == "proposed"
    verdicts = [e.payload for e in db.query(DeskEvent).filter(DeskEvent.kind == "guard_verdict")]
    assert '"pot_exceeded"' in verdicts[-1]  # still recorded as a live-only reason


# 3. A knock-out with no bid and the underlying past its barrier closes at 0.
def test_a_knocked_out_product_closes_at_zero(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p = _open_ko(db, user)
    no_bid = _sc(quotes={})
    monkeypatch.setattr(service, "underlying_price", lambda *a, **k: Decimal("95"))
    assert service.manage_open_positions(db, no_bid.cli(), NOW, {}) == [] and p.status == "open"
    monkeypatch.setattr(service, "underlying_price", lambda *a, **k: Decimal("85"))
    closed = service.manage_open_positions(db, no_bid.cli(), NOW, {})
    assert [r for _, r in closed] == ["knock_out"]
    assert p.exit_price == 0 and p.net_eur == Decimal("-11.98")


def test_owner_close_of_a_knocked_out_short_closes_at_zero(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p = _open_ko(db, user, barrier="110", direction="short")
    monkeypatch.setattr(service, "underlying_price", lambda *a, **k: Decimal("112"))
    assert service.owner_close(db, p, _sc(quotes={}).cli(), NOW) is True
    assert (p.exit_reason, p.exit_price) == ("knock_out", Decimal(0))


# 9. /close does not fill at a stale quote.
def test_owner_close_refuses_a_stale_quote():
    db = _memory_db()
    user = make_user(db)
    p = _open_ko(db, user)
    stale = _sc(now=NOW - timedelta(minutes=5), quotes={KO: ("0.95", "0.96")})
    assert service.owner_close(db, p, stale.cli(), NOW) is False and p.status == "open"
    fresh = _sc(quotes={KO: ("0.95", "0.96")})
    assert service.owner_close(db, p, fresh.cli(), NOW) is True and p.exit_price == Decimal("0.95")


# 4. A timeout reading SEC's ticker file does not stop the news wire.
def test_an_sec_timeout_keeps_the_last_cik_map(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(newswire, "_cik_cache", {"at": 0.0, "map": {"0000000001": "NVDA"}})

    def timeout(*a, **k):
        raise httpx.ReadTimeout("sec")

    monkeypatch.setattr("app.foundation.data_engineering.sec_8k.cik_map", timeout)
    assert newswire.holding_ciks(db, {"NVDA": "holding:NVDA"}) == {"0000000001": "NVDA"}


# 5. The 30-day cleanup keeps each source's newest row, so a quiet SEC month is not a "first read".
def test_prune_keeps_the_newest_row_of_each_source():
    db = _memory_db()
    now = NOW.astimezone(UTC)

    def row(id_, source, days):
        db.add(NewsWireItem(id=id_, source=source, headline=id_, received_at=now - timedelta(days=days)))

    row("sec_8k:old", "sec_8k", 50)
    row("sec_8k:older", "sec_8k", 60)
    row("fed:old", "fed", 40)
    row("fed:new", "fed", 1)
    db.commit()
    newswire.prune(db, now)
    assert sorted(r.id for r in db.query(NewsWireItem)) == ["fed:new", "sec_8k:old"]


# 6. The CFD line is built after the tick's commit, and never for a tick that failed.
def test_fill_messages_are_built_after_the_commit(monkeypatch):
    db = _memory_db()
    make_user(db)
    log: list[str] = []
    monkeypatch.setattr("app.decision.desk.signals.index_momentum.fire_due_signals", lambda *a, **k: ["DAX"])
    monkeypatch.setattr(jobs, "_announce_fills", lambda db_, now: log.append("announce") or ["CFD: BUY"])
    monkeypatch.setattr("app.decision.desk.telegram.alert", lambda db_, uid, text: log.append(text))
    real_commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: (log.append("commit"), real_commit())[1])
    jobs.monitor_tick(db, NOW, _sc().cli())
    assert log[log.index("announce") - 1] == "commit" and log[-1] == "CFD: BUY"

    log.clear()

    def down(*a, **k):
        raise httpx.ConnectTimeout("alpaca")

    monkeypatch.setattr("app.decision.desk.cfd.manage", down)
    with pytest.raises(httpx.ConnectTimeout):
        jobs.monitor_tick(db, NOW + timedelta(minutes=1), _sc(NOW + timedelta(minutes=1)).cli())
    assert "announce" not in log


# 7. A tap and the tick use the stock's own exchange, not Xetra.
def test_a_us_stock_keeps_its_exchange_in_taps_and_ticks(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    _open_ko(db, user)
    proposed = DeskPosition(user_id=user.id, mode=service.MODE, signal="owner_call", cell="owner_call:knockout",
                            product_type="knockout", direction="long", isin="DE000KO000C3",
                            underlying_isin=US_STOCK.isin, underlying_label="Fake Corp", status="proposed", liquid=True)
    db.add(proposed)
    db.commit()
    seen: list[str] = []

    def take(db_, pid, nonce, cli, now, underlying=None):
        seen.append(underlying.exchange)
        return service.TapResult("unknown", message="x")

    monkeypatch.setattr(service, "take", take)
    monkeypatch.setattr("app.foundation.scalable.service.make_cli", lambda db_: None)
    telegram.handle_take(db, SimpleNamespace(user_id=user.id), "1", f"desk:take:{proposed.id}:n")

    def manage(db_, cli, now, underlyings):
        seen.append(underlyings[US_STOCK.isin].exchange)
        return []

    monkeypatch.setattr(service, "manage_open_positions", manage)
    jobs.monitor_tick(db, NOW, _sc().cli())
    assert seen == ["XNYS", "XNYS"]


# 8. A database error in a button handler is rolled back, so the tap is still answered.
def test_a_failed_button_handler_rolls_back_and_answers(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    answered: list[str] = []

    def broken(db_, account, chat_id, data):
        db_.add(User(id=user.id, username="dup", password_hash="x", role="admin"))
        db_.flush()  # primary key clash: the session now needs a rollback

    def answer(db_, callback_id, text):
        db_.query(User).count()  # PendingRollbackError without the rollback
        answered.append(text)

    monkeypatch.setattr(telegram_updates, "_callback_handlers", {"zz": broken})
    monkeypatch.setattr(telegram_updates, "get_account_by_chat_id", lambda db_, chat: SimpleNamespace(user_id=user.id))
    monkeypatch.setattr(telegram_updates.telegram_bot, "answer_callback_query", answer)
    telegram_updates.handle_callback_query(db, {"id": "c1", "data": "zz:1", "message": {"chat": {"id": 1}}})
    assert answered == ["Something went wrong."]


# 10. Every desk test file has its own _memory_db (AGENTS.md).
def test_desk_test_files_define_their_own_memory_db():
    here = Path(__file__).parent
    assert "def memory_db" not in (here / "desk_helpers.py").read_text()
    for f in here.glob("test_desk*.py"):
        text = f.read_text()
        if "_memory_db()" in text:
            assert "def _memory_db():" in text, f.name
