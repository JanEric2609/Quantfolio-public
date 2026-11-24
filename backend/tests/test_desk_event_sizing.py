from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import bankroll, event_sizing
from app.decision.desk import cfd_instruments as ci
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskCfdJournal

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_index_sizing_loses_about_risk_at_stop():
    s = event_sizing.size(ci.by_key("DAX"), Decimal("20000"), Decimal("200"), Decimal(1), NOW, "long")
    assert s is not None
    assert Decimal("4.5") <= s.loss_at_stop_eur <= Decimal("5.0")
    assert s.leverage == Decimal("20.0") and s.stop_level == Decimal("19800")


def test_us_stock_has_fx_and_short_stop_above():
    inst = ci.by_key("US:NVDA")
    s = event_sizing.size(inst, Decimal("500"), Decimal("20"), Decimal("0.9"), NOW, "short")
    assert s is not None and s.stop_level == Decimal("520")
    assert s.leverage == Decimal("5.0") and s.margin_eur == (s.exposure_eur * Decimal("0.2")).quantize(Decimal("0.01"))
    assert s.loss_at_stop_eur <= Decimal("5.00")


def test_de_stock_rounds_down_to_step():
    s = event_sizing.size(ci.by_key("DE:SAP"), Decimal("186.5"), Decimal("4"), Decimal(1), NOW)
    assert s is not None and s.quantity == s.quantity.quantize(Decimal("0.0001"))


def test_none_cases():
    inst = ci.by_key("US:NVDA")
    assert event_sizing.size(inst, Decimal("500"), Decimal("0"), Decimal(1), NOW) is None
    assert event_sizing.size(inst, Decimal("500"), Decimal("1000000"), Decimal(1), NOW) is None  # qty rounds to 0
    # margin cap: a tiny stop makes a huge exposure
    assert event_sizing.size(inst, Decimal("500"), Decimal("0.5"), Decimal(1), NOW) is None
    ok = event_sizing.size(inst, Decimal("500"), Decimal("25"), Decimal(1), NOW)
    assert ok is not None
    assert event_sizing.size(inst, Decimal("500"), Decimal("25"), Decimal(1), NOW,
                             open_margin_eur=Decimal("139")) is None


def _row(db, status, pnl=None, closed=None):
    db.add(DeskCfdJournal(position_id=f"p{db.query(DeskCfdJournal).count()}", user_id="u", market_key="SPX",
                          direction="long", currency="USD", status=status, real_pnl_eur=pnl, closed_at=closed))
    db.flush()


def _db_with_user():
    db = _memory_db()
    from app.foundation.models.entities import DeskPosition
    return db


def test_guard_cases():
    db = _memory_db()
    # no FK enforcement in sqlite memory by default
    assert bankroll.guard(db, "u", NOW).ok
    _row(db, "closed", Decimal("-20"), datetime(2026, 11, 3, 10, tzinfo=UTC))
    g = bankroll.guard(db, "u", NOW)
    assert g.ok and g.month_eur == Decimal("-20")
    _row(db, "closed", Decimal("-16"), datetime(2026, 11, 24, 9, tzinfo=UTC))
    g = bankroll.guard(db, "u", NOW)
    assert not g.ok and "today" in g.reason and g.day_eur == Decimal("-16")
    _row(db, "closed", Decimal("-30"), datetime(2026, 11, 10, 9, tzinfo=UTC))
    g = bankroll.guard(db, "u", NOW)
    assert not g.ok and "month" in g.reason and g.resumes == "2026-12-01"


def test_guard_open_and_other_month():
    db = _memory_db()
    _row(db, "closed", Decimal("-100"), datetime(2026, 10, 30, 9, tzinfo=UTC))
    assert bankroll.guard(db, "u", NOW).ok
    for _ in range(3):
        _row(db, "entered")
    g = bankroll.guard(db, "u", NOW)
    assert not g.ok and "open" in g.reason


def test_overnight_budget_covers_every_night_to_the_exit():
    from zoneinfo import ZoneInfo

    from app.decision.desk import events

    berlin = ZoneInfo("Europe/Berlin")
    fri = datetime(2026, 11, 20, 17, 10, tzinfo=berlin)
    assert events.planned_nights(fri, datetime(2026, 11, 23, 17, 10, tzinfo=berlin)) == 3  # Friday to Monday
    assert events.planned_nights(fri, fri) == 0  # out the same day
    inst = ci.by_key("DE:SAP")
    one = event_sizing.size(inst, Decimal("200"), Decimal("10"), Decimal(1), fri, "long", nights=1)
    ten = event_sizing.size(inst, Decimal("200"), Decimal("10"), Decimal(1), fri, "long", nights=14)
    assert ten.quantity < one.quantity and ten.loss_at_stop_eur <= Decimal("5")
    # the loss at the stop on the last night, every night charged, stays within the 5 EUR risk
    nightly = inst.overnight_rate("long") * Decimal("200")
    worst = ten.quantity * (Decimal("10") + inst.spread_bp / Decimal(10000) * Decimal("200") + nightly * 14)
    assert worst <= Decimal("5")
