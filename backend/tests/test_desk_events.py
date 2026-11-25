from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from desk_helpers import make_user
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk.cell_names import cell_name
from app.decision.desk import cfd, cfd_instruments, cfd_journal, event_messages, events, prereg_events, service
from app.decision.desk import cfd_instruments as ci
from app.decision.desk.event_limits import EVENT_LIMITS
from app.decision.desk.event_sources import Candidate
from app.foundation import desk_chain
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskPosition

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 18, 11, 0, tzinfo=BERLIN)  # Wednesday, Xetra open
FX = Decimal(1)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _cand(key="DE:SAP", cell="ta_trend:cfd", direction="long", ref="100", dist="2", now=NOW, days=5):
    inst = ci.by_key(key)
    return Candidate(cell=cell, inst=inst, direction=direction, ref_price=Decimal(ref), stop_distance=Decimal(dist),
                     max_exit_at=now + timedelta(days=days), reason="EMA 20 crossed above EMA 50.",
                     evidence={"stop_atr": 2.0})


def _setup(monkeypatch=None):
    db = _memory_db()
    user = make_user(db)
    prereg_events.register_events(db, now=NOW)
    return db, user


def _open(db, user, cand, now=NOW):
    return events._open_one(db, user.id, cand, now, FX)


def test_event_cells_pinned():
    assert cfd.EVENT_CELL_NAMES == prereg_events.EVENT_CELLS
    assert cfd.EVENT_OWNER_CLOSE is events.owner_close


def test_every_cell_opens_edits_and_closes():
    db, user = _setup()
    keys = ["DE:SAP", "DE:SIE", "DE:ALV"]
    cells = prereg_events.EVENT_CELLS
    for i, cell in enumerate(cells):
        now = NOW
        if i == 3:  # three open at a time and four new a day: close the first three, go on the next day
            for p in events.open_event_positions(db):
                events.event_close(db, p, Decimal("101"), "time", NOW, ci.by_key(keys[0]))
        if i >= 3:
            now = NOW + timedelta(days=1)
        key = keys[i % 3] if i < 3 else ["DE:DTE", "DE:MBG", "DE:BAS"][i - 3]
        position, why = events._open_one(db, user.id, _cand(key, cell, now=now), now, FX)
        assert position is not None, (cell, why)
        assert position.prereg_hash == prereg_events.EVENT_HASH and position.would_take_live is False
        j = cfd_journal.for_position(db, position.id)
        assert j.market_key == key and j.quantity_suggested > 0 and j.exposure_eur > 0
        text = event_messages.live_text(db, position, j, now)
        assert "CFD, manual (Trading 212:" in text and f"Paper-scored as {cell_name(cell)}" in text
        assert "Quantity" in text and "Stop:" in text
    db.commit()
    ok, bad = desk_chain.verify_chain(db)
    assert ok and bad is None


def test_limits_max_open_max_new_and_repeat():
    db, user = _setup()
    for key in ("DE:SAP", "DE:SIE", "DE:ALV"):
        assert _open(db, user, _cand(key))[0] is not None
    assert _open(db, user, _cand("DE:DTE"))[1] == "max_open"
    # close one: its instrument stays blocked for 24 hours, another opens (4th new of the day)
    first = events.open_event_positions(db)[0]
    events.event_close(db, first, Decimal("100"), "time", NOW + timedelta(minutes=5), ci.by_key("DE:SAP"))
    assert _open(db, user, _cand("DE:SAP"))[1] == "instrument_busy"
    assert _open(db, user, _cand("DE:DTE"))[0] is not None
    for p in events.open_event_positions(db):
        events.event_close(db, p, Decimal("100"), "time", NOW + timedelta(minutes=10), ci.by_key("DE:SAP"))
    assert _open(db, user, _cand("DE:MBG"))[1] == "max_new_per_day"


def test_halted_and_session_and_sizing():
    db, user = _setup()
    assert _open(db, user, _cand(), now=NOW.replace(hour=20))[1] == "session_closed"
    assert _open(db, user, _cand(dist="100000"))[1] == "sizing"
    us = _cand("US:NVDA")
    assert events._open_one(db, user.id, us, NOW, None)[1] in ("session_closed", "no_fx")


def test_overnight_charged_friday_to_monday():
    db, user = _setup()
    fri = datetime(2026, 11, 20, 10, 0, tzinfo=BERLIN)
    position, why = events._open_one(db, user.id, _cand(now=fri), fri, FX)
    assert position is not None, why
    inst = ci.by_key("DE:SAP")
    mon = datetime(2026, 11, 23, 10, 0, tzinfo=BERLIN)
    assert events.event_close(db, position, Decimal("100"), "time", mon, inst)
    exposure = Decimal(position.entry_price) * Decimal(position.quantity)
    expected = inst.overnight_rate("long") * exposure * 3
    assert position.exit_fee == expected.quantize(Decimal("0.0001"))
    spread = Decimal(position.spread_pct) * exposure
    assert position.net_eur == (-spread - expected).quantize(Decimal("0.0001"))


def test_usd_result_pays_fx_fee():
    db, user = _setup()
    us_now = datetime(2026, 11, 18, 16, 30, tzinfo=BERLIN)
    position, why = events._open_one(db, user.id, _cand("US:NVDA", now=us_now), us_now, Decimal("0.9"))
    assert position is not None, why
    inst = ci.by_key("US:NVDA")
    events.event_close(db, position, Decimal("110"), "time", us_now, inst)
    exposure = Decimal(position.entry_price) * Decimal(position.quantity)
    gross = Decimal("0.1") * exposure
    assert position.exit_fee == (gross * Decimal("0.005")).quantize(Decimal("0.0001"))


def test_exit_reasons_stop_time_rollover_halt(monkeypatch):
    db, user = _setup()
    p1, _ = _open(db, user, _cand("DE:SAP"))
    p2, _ = _open(db, user, _cand("DE:SIE", dist="2"))
    reads = events.Reads(prices={"DE:SAP": Decimal("97"), "DE:SIE": Decimal("100.5")})
    out = events.Outcome()
    events._exits(db, NOW + timedelta(minutes=1), reads, False, out)
    assert out.closed == [(p1.id, "stop")]
    out = events.Outcome()
    events._exits(db, NOW + timedelta(days=6), reads, False, out)
    assert out.closed == [(p2.id, "time")]
    p3, _ = _open(db, user, _cand("DE:ALV"))
    events._exits(db, NOW + timedelta(minutes=1), events.Reads(prices={"DE:ALV": Decimal("100")}), True, events.Outcome())
    assert p3.status == "closed" and p3.exit_reason == "halt"


def test_write_phase_makes_no_network_read(monkeypatch):
    db, user = _setup()

    def boom(*a, **k):
        raise AssertionError("network read after the first write")

    monkeypatch.setattr(cfd_instruments, "reference_price", boom)
    monkeypatch.setattr(cfd, "_eur_per_usd", boom)
    import app.foundation.market as market

    monkeypatch.setattr(market, "history", boom)
    out = events.write_phase(db, user.id, NOW, events.Reads(candidates=[_cand()], fx=FX))
    assert len(out.opened) == 1
    texts = events.messages(db, NOW, out)
    assert len(texts) == 1 and texts[0].journal_id and texts[0].markup


def test_halted_desk_opens_nothing():
    db, user = _setup()
    service.set_halt(db, True, "test", "t", now=NOW)
    out = events.write_phase(db, user.id, NOW, events.Reads(candidates=[_cand()], fx=FX))
    assert out.opened == []


def test_bankroll_stop_makes_the_message_paper_only():
    from app.foundation.models.entities import DeskCfdJournal

    db, user = _setup()
    db.add(DeskCfdJournal(position_id="old", user_id=user.id, market_key="SPX", direction="long", currency="USD",
                          status="closed", real_pnl_eur=Decimal("-61"), closed_at=NOW))
    db.commit()
    position, _ = _open(db, user, _cand())
    text = event_messages.live_text(db, position, cfd_journal.for_position(db, position.id), NOW)
    assert "Bankroll stop" in text and "Quantity" not in text and "Stop:" not in text


def test_evaluation_time_and_due(monkeypatch):
    day = NOW.date()
    at = events.evaluation_time("XETR", day)
    assert at.astimezone(BERLIN).strftime("%H:%M") == "17:10"
    ny = events.evaluation_time("XNYS", day)
    assert ny.astimezone(BERLIN).strftime("%H:%M") == "21:40"
    db, _ = _setup()
    assert events.due_exchanges(db, at + timedelta(minutes=1)) == ["XETR"]
    assert events.due_exchanges(db, at - timedelta(minutes=1)) == []
    service.set_health(db, event_eval_XETR=day.isoformat())
    assert events.due_exchanges(db, at + timedelta(minutes=1)) == []


def test_expiry_warning():
    from datetime import date

    assert events.expiry_warning(date(2026, 11, 20)) == []
    assert events.expiry_warning(date(2026, 12, 20)) == []  # next contract is more than 14 days off
    assert set(events.expiry_warning(date(2027, 3, 10))) == set(cfd_instruments.INDEX_EXPIRY)
    assert EVENT_LIMITS.max_open == 3 and UTC


def test_run_stage_opens_technical_signals_once_a_day(monkeypatch):
    from datetime import date

    from app.decision.desk import event_rules, event_sources
    from app.foundation import technical as t

    db, user = _setup()
    closes = [100 + i * 0.5 for i in range(250)] + [200.0, 190.0]
    bars = [t.Bar(date(2025, 1, 1) + timedelta(days=i), c, c + 1, c - 1, c) for i, c in enumerate(closes)]
    monkeypatch.setattr(event_rules, "bars_today", lambda db, inst, ref, today: bars)
    monkeypatch.setattr(cfd_instruments, "reference_price", lambda *a, **k: Decimal("190"))
    monkeypatch.setattr(cfd, "_eur_per_usd", lambda db, now: Decimal("0.9"))
    monkeypatch.setattr(event_sources, "prepare_news", lambda *a, **k: [])
    at = events.evaluation_time("XETR", NOW.date()) + timedelta(minutes=1)
    texts = events.run_stage(db, user.id, None, at, [])
    assert 1 <= len(texts) <= 3
    opened = db.query(DeskPosition).filter(DeskPosition.status == "open").all()
    assert opened and all(p.cell == "ta_rsi2:cfd" or p.cell.startswith("ta_") for p in opened)
    assert service.health(db)["event_eval_XETR"] == NOW.date().isoformat()
    again = events.run_stage(db, user.id, None, at + timedelta(minutes=1), [])
    assert again == []  # the evaluation runs once per exchange and day


def test_index_entry_refused_in_the_last_hour_before_expiry():
    db, user = _setup()
    # DAX expires Thu 17 Dec 2026 at 13:00 Berlin: at 12:30 an entry would close for the roll on the next tick.
    last_hour = datetime(2026, 12, 17, 12, 30, tzinfo=BERLIN)
    assert _open(db, user, _cand("DAX", now=last_hour), now=last_hour)[1] == "rollover_soon"
    assert "within the hour" in events.SKIP_TEXT["rollover_soon"]
    # After the expiry the next contract trades: the US evaluation that evening may open an index position.
    evening = datetime(2026, 12, 17, 21, 40, tzinfo=BERLIN)
    position, why = events._open_one(db, user.id, _cand("NDX", now=evening), evening, Decimal("0.9"))
    assert position is not None, why
    reads = events.Reads(prices={"NDX": Decimal("100")})
    out = events.Outcome()
    events._exits(db, (evening + timedelta(minutes=1)).astimezone(UTC), reads, False, out)
    assert out.closed == []  # not closed for a roll that already happened


@pytest.mark.parametrize("later", [timedelta(hours=1, minutes=15), timedelta(days=1)])
def test_index_position_closes_for_the_roll_of_its_own_contract(later):
    db, user = _setup()
    entered = datetime(2026, 12, 16, 11, 0, tzinfo=BERLIN)
    position, why = _open(db, user, _cand("DAX", now=entered), now=entered)
    assert position is not None, why
    # 12:05 on expiry day (the roll point is 12:00, an hour before 13:00), or the day after when no price came in time.
    now = datetime(2026, 12, 17, 10, 50, tzinfo=BERLIN) + later
    out = events.Outcome()
    events._exits(db, now.astimezone(UTC), events.Reads(prices={"DAX": Decimal("100")}), False, out)
    assert out.closed == [(position.id, "rollover")]
