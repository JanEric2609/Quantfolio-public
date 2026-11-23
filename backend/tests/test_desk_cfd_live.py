"""One live CFD message per trade, grouped announcements (ADR 0027 amendment 2026-11-23, commit 4)."""
from datetime import UTC, timedelta
from decimal import Decimal
from types import SimpleNamespace

from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, make_user, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd, cfd_journal, jobs
from app.decision.desk.underlyings import ALIASES
from app.foundation import telegram_bot
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskPosition


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _ctx():
    return cfd.CfdContext(
        key="DAX", r1=Decimal("0.0042"), proxy_prev_close=Decimal(100), index_prev_close=Decimal(25000),
        index_level_est=Decimal(25000), currency="EUR", eur_per_unit=Decimal(1), sigma=Decimal("0.01"),
        session_minutes=510, enter_by=(NOW + timedelta(minutes=5)).astimezone(UTC), exit_at=(NOW + timedelta(minutes=27)).astimezone(UTC),
        exposure_eur=Decimal("500.00"), quantity_t212=Decimal("0.02"), stop_points=Decimal(250),
        typical_30m_points=Decimal(60), spread_points=Decimal("1.25"), margin_eur=Decimal(25),
        overnight_eur_per_night=Decimal("0.04"))


class Tg:
    """Records what would go to Telegram."""

    def __init__(self, monkeypatch):
        self.sent, self.edits, self.plain = [], [], []
        monkeypatch.setattr(telegram_bot, "get_linked_account", lambda db, uid: SimpleNamespace(chat_id="1"))
        monkeypatch.setattr(telegram_bot, "send_telegram_message_id",
                            lambda db, chat, text, markup=None: (self.sent.append((text, markup)), 700 + len(self.sent))[1])
        monkeypatch.setattr(telegram_bot, "edit_telegram_message",
                            lambda db, chat, mid, text, markup=None: (self.edits.append((mid, text, markup)), True)[1])
        monkeypatch.setattr(telegram_bot, "send_telegram_message",
                            lambda db, chat, text, markup=None: (self.plain.append(text), True)[1])


def _fire(db, user):
    """A CFD with its context, plus the paper knock-out and warrant of the same signal."""
    u = ALIASES["DAX"]
    cp = cfd.record_signal(db, user.id, "DAX", u, "long", NOW + timedelta(minutes=27), NOW, Decimal(100),
                           Decimal("0.01"), context=_ctx())
    ids = [cp.id]
    for kind, wkn in (("knockout", "KO000A"), ("warrant", "WA000A")):
        p = DeskPosition(user_id=user.id, cell=f"index_momentum:{kind}", signal="index_momentum", product_type=kind,
                         isin=f"DE000{wkn}0", wkn=wkn, underlying_isin=u.isin, underlying_label=u.label,
                         direction="long", units=2, entry_price=Decimal(1), entry_at=NOW.astimezone(UTC), status="open",
                         max_exit_at=NOW + timedelta(minutes=27))
        db.add(p)
        db.flush()
        ids.append(p.id)
    db.commit()
    return cp, ids


def test_signal_tick_sends_one_grouped_message_and_stores_its_id(monkeypatch):
    tg = Tg(monkeypatch)
    db = _memory_db()
    user = make_user(db)
    cp, ids = _fire(db, user)
    out = jobs._announce_fills(db, NOW, ids)
    assert len(out) == 1
    assert "DAX long: CFD, manual" in out[0] and "Also on paper (no action):" in out[0]
    assert "  Knock-out KO000A, 2 units" in out[0] and "  Call WA000A, 2 units" in out[0]
    jobs._send(db, user.id, out)
    j = cfd_journal.for_position(db, cp.id)
    assert j.telegram_message_id == 701 and tg.sent[0][1]["inline_keyboard"][0][0]["text"] == "Entered"
    # The next tick with the same price: no edit.
    jobs._live_edits(db, user.id, NOW + timedelta(minutes=1))
    assert tg.edits == []


def test_a_moved_price_edits_the_message(monkeypatch):
    tg = Tg(monkeypatch)
    db = _memory_db()
    user = make_user(db)
    cp, ids = _fire(db, user)
    jobs._send(db, user.id, jobs._announce_fills(db, NOW, ids))
    cp.last_bid = Decimal("100.08")  # +0.08 % -> about +20 index points
    db.commit()
    jobs._live_edits(db, user.id, NOW + timedelta(minutes=12))
    assert len(tg.edits) == 1
    mid, text, markup = tg.edits[0]
    assert mid == 701 and "Now (11:12): DAX ≈ 25,020 (+20 pts since the signal), paper +0.40 EUR before costs." in text
    assert "Exit 11:27 (in 15 min)." in text and "Also on paper (no action):" in text
    assert markup["inline_keyboard"][0][1]["text"] == "Skipped"
    jobs._live_edits(db, user.id, NOW + timedelta(minutes=12))  # same text again: nothing
    assert len(tg.edits) == 1


def test_status_changes_change_the_buttons(monkeypatch):
    tg = Tg(monkeypatch)
    db = _memory_db()
    user = make_user(db)
    cp, ids = _fire(db, user)
    jobs._send(db, user.id, jobs._announce_fills(db, NOW, ids))
    j = cfd_journal.for_position(db, cp.id)
    cfd_journal.mark_entered(db, j, NOW, Decimal("25010.5"))
    db.commit()
    jobs._live_edits(db, user.id, NOW + timedelta(minutes=1))
    _, text, markup = tg.edits[-1]
    assert "Your trade: entered 25,010.5 × 0.0200, stop 24,760.5" in text
    assert markup["inline_keyboard"][0][0]["text"] == "Closed"
    cfd_journal.mark_closed(db, j, NOW, Decimal("25020"))
    db.commit()
    jobs._live_edits(db, user.id, NOW + timedelta(minutes=2))
    assert tg.edits[-1][2] == {"inline_keyboard": []} or not tg.edits[-1][2]  # no buttons after Closed


def test_skipped_removes_the_buttons_and_the_window_passed_note_appears(monkeypatch):
    tg = Tg(monkeypatch)
    db = _memory_db()
    user = make_user(db)
    cp, ids = _fire(db, user)
    jobs._send(db, user.id, jobs._announce_fills(db, NOW, ids))
    jobs._live_edits(db, user.id, NOW + timedelta(minutes=6))
    assert "Entry window passed (11:05). Skip unless you are already in." in tg.edits[-1][1]
    cfd_journal.mark_skipped(db, cfd_journal.for_position(db, cp.id), NOW)
    db.commit()
    jobs._live_edits(db, user.id, NOW + timedelta(minutes=7))
    assert "Your trade: skipped" in tg.edits[-1][1] and not tg.edits[-1][2]


def test_exit_tick_sends_a_new_close_message_and_a_final_edit(monkeypatch):
    tg = Tg(monkeypatch)
    db = _memory_db()
    user = make_user(db)
    cp, ids = _fire(db, user)
    jobs._send(db, user.id, jobs._announce_fills(db, NOW, ids))
    cfd_journal.mark_entered(db, cfd_journal.for_position(db, cp.id), NOW, Decimal("25000"))
    db.commit()
    exit_at = NOW + timedelta(minutes=27)
    sc = FakeSc(standard_handler({DAX_ETF: ("100.2", "100.2")}, chart_pts=daily_points(), now=exit_at))
    jobs.monitor_tick(db, exit_at, sc.cli())
    assert any("DAX CFD: CLOSE now (11:27, the rule's exit)" in t for t in tg.plain)
    assert not any("DAX CFD: CLOSE" in t for _, t in [(0, e[1]) for e in tg.edits])
    final = tg.edits[-1][1]
    assert final.startswith("CLOSED 11:27: paper result") and "Your trade: entered 25,000.0" in final
    assert tg.edits[-1][2]["inline_keyboard"][0][0]["text"] == "Closed"  # the owner has not closed theirs yet
