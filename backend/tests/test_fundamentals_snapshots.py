from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_backbone.fundamentals_snapshots import snapshot_fundamentals
from app.foundation.models.entities import Fundamental, FundamentalsSnapshot


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _row(db, ticker, data, fetched):
    db.add(Fundamental(ticker=ticker, data_json=data, fetched_at=fetched, source="test"))
    db.commit()


def test_copies_fresh_rows_under_their_fetch_day_once():
    db = _memory_db()
    now = datetime(2026, 11, 11, 4, 50, tzinfo=UTC)
    _row(db, "AAA", '{"pe": 12}', now - timedelta(hours=10))
    first = snapshot_fundamentals(db, now=now)
    again = snapshot_fundamentals(db, now=now + timedelta(hours=1))
    rows = db.query(FundamentalsSnapshot).all()
    assert first["stored"] == 1 and again["stored"] == 0
    assert [(r.symbol, str(r.snapshot_date)) for r in rows] == [("AAA", "2026-11-10")]


def test_skips_empty_and_old_rows_and_keeps_history_when_values_change():
    db = _memory_db()
    now = datetime(2026, 11, 11, 4, 50, tzinfo=UTC)
    _row(db, "EMPTY", "{}", now)
    _row(db, "OLD", '{"pe": 9}', now - timedelta(days=10))
    _row(db, "AAA", '{"pe": 12}', now - timedelta(days=1))
    snapshot_fundamentals(db, now=now)
    row = db.query(Fundamental).filter_by(ticker="AAA").one()
    row.data_json, row.fetched_at = '{"pe": 15}', now
    db.commit()
    snapshot_fundamentals(db, now=now)
    got = sorted((r.symbol, str(r.snapshot_date), r.data_json) for r in db.query(FundamentalsSnapshot).all())
    assert got == [("AAA", "2026-11-10", '{"pe": 12}'), ("AAA", "2026-11-11", '{"pe": 15}')]
