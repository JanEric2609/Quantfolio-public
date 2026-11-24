from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import prereg, prereg_events, scoring, scoring_events
from app.decision.desk.event_limits import EVENT_LIMITS
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskPosition, DeskRegistration


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_first_registration_hash_unchanged():
    assert prereg.PREREG_HASH == "86396b951089aa96007d6227bbfda59991be23b7b8b8ecd2975af6afa5c4a8cd"


def test_event_hash_pinned():
    assert prereg_events.EVENT_HASH == "0350c35d98bb5e06f434c7a6e2858b2c2fc69e6942dece4978265ad7da38e579"


def test_limits_pinned():
    assert EVENT_LIMITS.as_json() == {
        "pot": "280", "risk_per_trade": "5", "daily_real_loss_stop": "15", "monthly_real_loss_stop": "60",
        "max_open": 3, "max_new_per_day": 4, "max_margin_share": "0.5", "max_hold_trading_days": 10,
        "min_minutes_between_same_instrument": 1440}


def test_register_events_idempotent():
    db = _memory_db()
    now = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    prereg_events.register_events(db, now=now)
    prereg_events.register_events(db, now=now)
    assert db.query(DeskRegistration).filter_by(prereg_hash=prereg_events.EVENT_HASH).count() == 1
    from app.foundation.models.entities import DeskEvent
    assert db.query(DeskEvent).count() == 1
    assert prereg_events.registered_at_events(db) is not None


def _closed(db, cell, h, net=1.0):
    db.add(DeskPosition(
        user_id="u", mode="paper", cell=cell, signal="x", product_type="cfd", isin="US1", underlying_isin="US1",
        underlying_label="X", direction="long", liquid=True, quantity=Decimal("1"), entry_price=Decimal("10"),
        entry_at=datetime(2026, 11, 25, 10, tzinfo=UTC), status="closed", net_eur=Decimal(str(net)),
        underlying_move=Decimal("0.01"), prereg_hash=h, product_name="p", leverage_or_omega=Decimal(1)))


def test_scorecards_are_separate():
    db = _memory_db()
    now = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    prereg.register(db, now=now)
    prereg_events.register_events(db, now=now)
    _closed(db, "ta_trend:cfd", prereg_events.EVENT_HASH)
    _closed(db, "index_momentum:cfd", prereg.PREREG_HASH)
    db.commit()
    ev = scoring_events.scorecard_events(db)
    old = scoring.scorecard(db)
    assert ev["cells"]["ta_trend:cfd"]["n"] == 1 and "index_momentum:cfd" not in ev["cells"]
    assert old["cells"]["index_momentum:cfd"]["n"] == 1 and "ta_trend:cfd" not in old["cells"]
