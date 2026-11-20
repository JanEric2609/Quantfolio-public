"""Accept links to the broker; the next sync finds the trade; nothing is logged by hand."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import DkbAccount, DkbPosition, Recommendation, RecommendationReview, User
from app.foundation.recommendation_execution import (
    accepted_and_executed,
    broker_links,
    detect_executions,
    record_acceptance,
)

ISIN = "IE00B4L5Y983"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _setup(db, qty: str = "10"):
    user = User(username=f"u{uuid4().hex[:6]}", password_hash="x")
    db.add(user)
    db.commit()
    depot = DkbAccount(id=uuid4().hex, user_id=user.id, type="depot", iban="DE" + uuid4().hex[:20],
                       balance=Decimal("0"), currency="EUR")
    db.add(depot)
    db.commit()
    pos = DkbPosition(id=uuid4().hex, account_id=depot.id, isin=ISIN, ticker="EUNL.DE", name="World",
                      quantity=Decimal(qty), avg_buy_price=Decimal("90"), current_price=Decimal("100"),
                      current_value=Decimal(qty) * 100)
    db.add(pos)
    rec = Recommendation(user_id=user.id, ticker="EUNL.DE", verdict="BUY", confidence=Decimal("0.6"),
                         payload_json=json.dumps({"candidate": {"isin": ISIN, "name": "World"}}),
                         approval_state="accepted", mode="discover")
    db.add(rec)
    db.commit()
    return user, pos, rec


def test_links_open_the_security_at_each_broker():
    links = {link["broker"]: link["url"] for link in broker_links(ISIN)}
    assert links["dkb"].endswith(f"isin={ISIN}")
    assert links["scalable"] == f"https://de.scalable.capital/broker/security?isin={ISIN}"
    assert [link["broker"] for link in broker_links(None)] == ["dkb"]


def test_a_buy_shows_up_as_more_units_after_the_sync():
    db = _memory_db()
    user, pos, rec = _setup(db)
    record_acceptance(db, rec)
    db.commit()
    assert detect_executions(db, user.id) == []  # nothing bought yet

    pos.quantity = Decimal("13")  # the next DKB sync
    db.commit()
    found = detect_executions(db, user.id)

    assert found == [{"id": rec.id, "ticker": "EUNL.DE", "broker": "dkb", "units": 3.0}]
    db.refresh(rec)
    assert rec.approval_state == "executed"
    review = db.query(RecommendationReview).one()
    assert review.to_state == "executed" and "DKB sync: +3 units" in review.notes
    lists = accepted_and_executed(db, user.id)
    assert lists["waiting"] == [] and lists["executed"][0]["broker_label"] == "DKB"


def test_a_sale_of_the_held_units_does_not_count_as_the_buy():
    db = _memory_db()
    user, pos, rec = _setup(db)
    record_acceptance(db, rec)
    db.commit()
    pos.quantity = Decimal("4")
    db.commit()
    assert detect_executions(db, user.id) == []
    assert accepted_and_executed(db, user.id)["waiting"][0]["id"] == rec.id


def test_an_old_acceptance_is_no_longer_watched():
    db = _memory_db()
    user, pos, rec = _setup(db)
    record_acceptance(db, rec, now=datetime.now(UTC) - timedelta(days=90))
    db.commit()
    pos.quantity = Decimal("20")
    db.commit()
    assert detect_executions(db, user.id) == []
