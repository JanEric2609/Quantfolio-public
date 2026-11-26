import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd_instruments as ci
from app.decision.desk import event_sources as es
from app.decision.desk import events, prereg_events
from app.decision.desk.cell_names import cell_name
from app.decision.desk.event_sources import Candidate
from app.foundation import technical
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskPosition, NewsWireItem

NOW = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)  # 11:00 New York, a Tuesday
TODAY = date(2026, 11, 24)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _bars(move=0.02, close_pos=1.0):
    """Forty calm days (about 0.5 % a day) and today's bar: *move* from the last close, closing at *close_pos* of its range."""
    out, price = [], 5000.0
    for k in range(40, 0, -1):
        d = TODAY - timedelta(days=k)
        if d.weekday() > 4:
            continue
        o, c = price, price * (1.005 if k % 2 else 0.995)
        out.append(technical.Bar(d, o, max(o, c) * 1.002, min(o, c) * 0.998, c))
        price = c
    prev = out[-1].close
    close = prev * (1 + move)
    high, low = max(close, prev) * 1.0005, min(close, prev) * 0.9995
    if move > 0:
        close = low + (high - low) * close_pos
    out.append(technical.Bar(TODAY, prev, high, low, close))
    return out


def _patch(monkeypatch, bars, macro=("CPI",)):
    monkeypatch.setattr(ci, "daily_bars", lambda db, inst, days=400: bars)
    monkeypatch.setattr(ci, "reference_price", lambda db, cli, inst, now, errors=None: Decimal("500"))
    monkeypatch.setattr(es.cal, "macro_events_between", lambda a, b: [(k, a.isoformat()) for k in macro])


def _news(db, i, source, materiality, direction, scope="market", minutes=10):
    db.add(NewsWireItem(id=f"{source}:{i}", source=source, headline=f"headline {i}", symbols="", received_at=NOW - timedelta(minutes=minutes),
                        materiality=materiality, direction=direction, scope=scope))
    db.commit()


def test_cell_is_registered_and_named():
    assert "strong_event:cfd" in prereg_events.EVENT_CELLS and len(prereg_events.EVENT_CELLS) == 7
    assert cell_name("strong_event:cfd") == "strong event"
    assert prereg_events.EVENT_SPEC["rules"]["strong_event"]["target_r"] == 2.0


def test_fires_on_a_held_move_with_two_extra_signals(monkeypatch):
    db = _memory_db()
    _patch(monkeypatch, _bars(0.02))
    _news(db, 1, "alpaca", 3, "up")  # headline rated 3 in the move's direction; macro day = second signal
    out = es.prepare_strong(db, None, NOW)
    assert out and all(c.cell == "strong_event:cfd" and c.direction == "long" for c in out)
    c = out[0]
    assert c.stop_distance > 0 and c.evidence["signals"]["headline"] and c.evidence["signals"]["macro"]
    assert "strong event" in c.reason and c.max_exit_at > NOW


def test_needs_move_holding_and_two_signals(monkeypatch):
    db = _memory_db()
    _news(db, 1, "alpaca", 3, "up")
    _patch(monkeypatch, _bars(0.002))  # too small a move
    assert es.prepare_strong(db, None, NOW) == []
    _patch(monkeypatch, _bars(0.02, close_pos=0.4))  # gave back more than a quarter
    assert es.prepare_strong(db, None, NOW) == []
    _patch(monkeypatch, _bars(0.02), macro=())  # headline only: one signal
    assert es.prepare_strong(db, None, NOW) == []
    _patch(monkeypatch, _bars(0.02))
    db.query(NewsWireItem).delete()
    db.commit()
    _news(db, 2, "alpaca", 3, "down")  # headline points the other way
    assert es.prepare_strong(db, None, NOW) == []


def test_two_independent_sources_count_as_one_signal(monkeypatch):
    db = _memory_db()
    _patch(monkeypatch, _bars(0.02), macro=())
    _news(db, 1, "alpaca", 2, "up")
    _news(db, 2, "fed", 2, "up")
    assert es.prepare_strong(db, None, NOW) == []  # one signal (two sources), no headline rated 3, no macro day
    _news(db, 3, "ecb", 3, "up")
    assert es.prepare_strong(db, None, NOW)  # headline + second source


def test_short_on_a_down_move_and_weekly_cap(monkeypatch):
    db = _memory_db()
    down = _bars(-0.02)
    down[-1] = technical.Bar(down[-1].date, down[-1].open, down[-1].high, down[-1].low, down[-1].low)
    _patch(monkeypatch, down)
    _news(db, 1, "alpaca", 3, "down")
    assert es.prepare_strong(db, None, NOW)[0].direction == "short"
    for i in range(3):
        db.add(DeskPosition(user_id="u", mode="paper", cell="strong_event:cfd", signal="strong_event", product_type="cfd", isin="X",
                            underlying_isin=f"I{i}", direction="long", created_at=NOW - timedelta(days=1)))
    db.commit()
    assert es.prepare_strong(db, None, NOW) == []


def _position(db, direction="long", entry="500", initial_stop="490", stop=None):
    from app.decision.desk.ledger import append_event
    p = DeskPosition(user_id="u", mode="paper", cell="strong_event:cfd", signal="strong_event", product_type="cfd", isin="X",
                     underlying_isin="I", direction=direction, entry_price=Decimal(entry), stop_price=Decimal(stop or initial_stop),
                     status="open")
    db.add(p)
    db.flush()
    append_event(db, "fill", "paper", {"stop": initial_stop}, p.id, now=NOW)
    return p


def test_target_and_trailing_stop_exit_on_price():
    db = _memory_db()
    p = _position(db)  # risk 10: target at +20, trail after +10 and 10 behind the best price
    assert events._strong_exit(db, p, Decimal("505")) is None and p.stop_price == Decimal("490")
    assert events._strong_exit(db, p, Decimal("512")) is None and p.stop_price == Decimal("502.0000")  # trailed up
    assert events._strong_exit(db, p, Decimal("508")) is None and p.stop_price == Decimal("502.0000")  # never loosens
    assert events._stopped(p, Decimal("501.9")) is True
    assert events._strong_exit(db, p, Decimal("520")) == "target"


def test_short_trails_down_and_signal_gone_exit():
    db = _memory_db()
    p = _position(db, "short", entry="500", initial_stop="510")
    events._strong_exit(db, p, Decimal("488"))
    assert p.stop_price == Decimal("498.0000")
    assert events._strong_exit(db, p, Decimal("480")) == "target"
    up, dn = _bars(0.02), _bars(-0.02)
    assert technical.exit_hit("strong_event:cfd", "long", up) is None
    gone = [*up[:-1], technical.Bar(up[-1].date, up[-1].open, up[-1].high, up[-1].low, up[-2].close * 0.999)]
    assert technical.exit_hit("strong_event:cfd", "long", gone) == "signal_gone"
    assert technical.exit_hit("strong_event:cfd", "short", dn) is None
    assert json.dumps(technical.EVENT_RULES["strong_event"])  # JSON-clean for the hash
