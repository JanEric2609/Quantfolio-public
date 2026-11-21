"""Regression tests for the seventh 2026-11-20 review of the derivatives desk (pullfrog: lock order)."""
from datetime import UTC, timedelta
from decimal import Decimal
from types import SimpleNamespace

from desk_helpers import NOW, make_user
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import ledger, service
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskPosition


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_the_write_lock_is_a_transaction_advisory_lock_on_postgres_and_a_no_op_on_sqlite():
    sql: list[str] = []
    fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
                           execute=lambda stmt, params=None: sql.append(f"{stmt} {params}"))
    ledger.desk_write_lock(fake)
    assert sql == ["SELECT pg_advisory_xact_lock(hashtext(:name)) {'name': 'desk_ledger_write'}"]
    ledger.desk_write_lock(_memory_db())  # SQLite: nothing to lock, no error


def test_every_desk_write_path_takes_the_write_lock_before_a_position_row(monkeypatch):
    """One lock order everywhere (write lock, row, ledger seq): the monitor and an owner's Close cannot
    deadlock (pullfrog, service.py:487)."""
    db = _memory_db()
    user = make_user(db)
    order: list[str] = []
    monkeypatch.setattr(service, "desk_write_lock", lambda db_: order.append("lock"))
    monkeypatch.setattr(ledger, "desk_write_lock", lambda db_: order.append("lock"))
    real_query = db.query

    def query(*a, **k):
        order.append("row")
        return real_query(*a, **k)

    open_p = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                          isin="DE000KO000A1", underlying_isin=ALIASES["DAX"].isin, direction="long", status="open",
                          units=1, entry_price=Decimal(1), entry_at=NOW.astimezone(UTC))
    db.add(open_p)
    db.commit()
    monkeypatch.setattr(db, "query", query)
    assert service.claim_open(db, open_p) is True
    assert order[:2] == ["lock", "row"]
    order.clear()
    service.expire_proposals(db, NOW + timedelta(minutes=5))
    assert order[:2] == ["lock", "row"]
    order.clear()
    real_get = db.get
    monkeypatch.setattr(db, "get", lambda *a, **k: order.append("row") or real_get(*a, **k))
    service.take(db, "missing", "n", None, NOW)
    assert order[:2] == ["lock", "row"]
