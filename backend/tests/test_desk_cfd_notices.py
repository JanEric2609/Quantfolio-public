"""Heads-up and get-ready messages: a read-only preview of the rule (ADR 0027 amendment 2026-11-23)."""
from decimal import Decimal

from desk_helpers import BERLIN, NOW, FakeSc, make_user, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import calendar as cal
from app.decision.desk import service
from app.decision.desk.signals import index_momentum as im
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent

DAY = NOW.date()


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _at(h, m):
    return NOW.replace(hour=h, minute=m)


def _x(out):
    """Drop the US group's own heads-up (the NYSE window opens at 16:05 Berlin)."""
    return [m for m in out if "S&P 500" not in m]


def _cli():
    return FakeSc(standard_handler({})).cli()


def test_heads_up_is_sent_once_and_recorded(monkeypatch):
    db = _memory_db()
    make_user(db)
    monkeypatch.setattr(im, "compute_r1", lambda db, cli, plan, now=None: Decimal("0.0042"))
    out = im.preview_due(db, _cli(), _at(9, 50))
    assert len(out) == 1 and out[0].splitlines()[0] == "Today's CFD plan (paper rule, manual on Trading 212):"
    assert "DAX: long at 17:00 Berlin (r1 +0.42 %), market Germany 40 (GER40)" in out[0]
    assert "Euro Stoxx 50: long at 17:00 Berlin" in out[0]
    assert out[0].endswith("The final call with size and stop comes at 17:00.")
    notices = service.health(db)["cfd_notices"]
    assert notices[f"XETR:{DAY}:headsup"]["DAX"] == {"dir": "long", "r1": "0.0042"}
    assert im.preview_due(db, _cli(), _at(9, 51)) == []


def test_window_bounds_for_the_heads_up(monkeypatch):
    db = _memory_db()
    make_user(db)
    monkeypatch.setattr(im, "compute_r1", lambda db, cli, plan, now=None: Decimal("-0.01"))
    assert im.preview_due(db, _cli(), _at(9, 44)) == []  # before open + 45 min
    assert _x(im.preview_due(db, _cli(), _at(16, 51))) == []  # after enter_at - 10 min
    assert len(im.preview_due(db, _cli(), _at(16, 49))) == 1


def test_a_failed_read_is_retried_and_not_marked(monkeypatch):
    db = _memory_db()
    make_user(db)
    calls = []

    def flaky(db, cli, plan, now=None):
        calls.append(plan.key)
        if len(calls) == 1:
            raise im.DataUnavailable("ReadTimeout")
        return Decimal("0.01")

    monkeypatch.setattr(im, "compute_r1", flaky)
    assert im.preview_due(db, _cli(), _at(10, 0)) == [] and "cfd_notices" not in service.health(db)
    assert len(im.preview_due(db, _cli(), _at(10, 1))) == 1


def test_a_macro_day_says_no_trade(monkeypatch):
    db = _memory_db()
    make_user(db)
    monkeypatch.setattr(cal, "macro_events_between", lambda a, b: [("ecb", DAY.isoformat())])
    out = im.preview_due(db, _cli(), _at(10, 0))
    assert "Euro Stoxx 50: no trade today (macro event: ECB)" in out[0] and "Events today: ECB" in out[0]
    assert service.health(db)["cfd_notices"][f"XETR:{DAY}:headsup"] == {}
    assert _x(im.preview_due(db, _cli(), _at(16, 56))) == []  # nothing fires: no get-ready


def test_get_ready_only_for_firing_plans_and_once(monkeypatch):
    db = _memory_db()
    make_user(db)
    monkeypatch.setattr(im, "compute_r1", lambda db, cli, plan, now=None: Decimal("0.01") if plan.key == "DAX" else Decimal("0"))
    im.preview_due(db, _cli(), _at(10, 0))
    out = _x(im.preview_due(db, _cli(), _at(16, 56)))
    assert out == ["In 5 minutes: DAX long expected. Open Trading 212 → Germany 40 (GER40)."]
    assert _x(im.preview_due(db, _cli(), _at(16, 57))) == []


def test_no_get_ready_when_no_heads_up_was_sent(monkeypatch):
    db = _memory_db()
    make_user(db)
    monkeypatch.setattr(im, "compute_r1", lambda *a, **k: Decimal("0.01"))
    assert _x(im.preview_due(db, _cli(), _at(16, 56))) == []  # the XETR window is over, no heads-up before


def test_the_preview_writes_no_ledger_row_and_does_not_fire_the_signal(monkeypatch):
    db = _memory_db()
    make_user(db)
    monkeypatch.setattr(im, "compute_r1", lambda *a, **k: Decimal("0.01"))
    im.preview_due(db, _cli(), _at(10, 0))
    im.preview_due(db, _cli(), _at(16, 56))
    db.commit()
    assert db.query(DeskEvent).count() == 0
    assert im._already_fired(db, f"DAX:{DAY.isoformat()}") is False
    enter = [p for p in im.plans_for_day(DAY) if p.key == "DAX"][0].enter_at
    assert enter.astimezone(BERLIN).strftime("%H:%M") == "17:00"
