"""A3: each month's plan actions are stored, and a placed order links back (ADR 0019 §3)."""
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.monthly_plan_store import open_actions_for, snapshot_monthly_plan
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    DkbAccount,
    DkbPosition,
    MonthlyPlanAction,
    Recommendation,
    User,
)
from app.foundation.recommendation_execution import detect_executions, execution_info, record_acceptance

_PREVIOUS = "0131_trust_factor_study"
_REVISION = "0132_monthly_plan_actions"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
ISIN = "IE00B4L5Y983"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def test_revision_chain_guards_and_length():
    text = Path("alembic/versions/0132_monthly_plan_actions.py").read_text()
    assert f'revision = "{_REVISION}"' in text
    assert f'down_revision = "{_PREVIOUS}"' in text
    assert len(_REVISION) <= 32
    assert '_TABLE = "monthly_plan_actions"' in text
    assert "has_table(_TABLE)" in text
    assert "drop_all" not in text


def test_upgrade_creates_and_downgrade_drops_the_table():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    cfg = _config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, _PREVIOUS)
        assert not sa.inspect(conn).has_table("monthly_plan_actions")

        command.upgrade(cfg, _REVISION)
        inspector = sa.inspect(conn)
        assert inspector.has_table("monthly_plan_actions")
        columns = {c["name"] for c in inspector.get_columns("monthly_plan_actions")}
        assert {"id", "user_id", "month", "sleeve", "kind", "status", "fulfilled_at"} <= columns

        command.downgrade(cfg, _PREVIOUS)
        assert not sa.inspect(conn).has_table("monthly_plan_actions")

        command.upgrade(cfg, _REVISION)
        assert sa.inspect(conn).has_table("monthly_plan_actions")


def _user_with_position(db, qty: str = "10"):
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
    db.commit()
    return user, pos


def test_snapshot_is_idempotent_per_month():
    db = _memory_db()
    user, _ = _user_with_position(db)
    first = snapshot_monthly_plan(db, user.id, now=NOW)
    assert len(first) >= 1
    assert {r.month for r in first} == {"2026-10"}
    assert db.query(MonthlyPlanAction).count() == len(first)
    second = snapshot_monthly_plan(db, user.id, now=NOW)
    assert db.query(MonthlyPlanAction).count() == len(first)
    assert {r.id for r in second} == {r.id for r in first}


def test_placed_order_links_back_to_its_plan_action():
    db = _memory_db()
    user, pos = _user_with_position(db)
    rec = Recommendation(user_id=user.id, ticker="EUNL.DE", verdict="BUY", confidence=Decimal("0.6"),
                         payload_json=json.dumps({"candidate": {"isin": ISIN, "name": "World"}}),
                         approval_state="accepted", mode="discover")
    db.add(rec)
    db.commit()
    record_acceptance(db, rec)
    db.commit()

    snapshot_monthly_plan(db, user.id, now=NOW)
    assert open_actions_for(db, user.id, isin=ISIN)

    pos.quantity = Decimal("13")
    db.commit()
    found = detect_executions(db, user.id)
    assert len(found) == 1

    db.refresh(rec)
    info = execution_info(rec)
    assert info is not None and info.get("plan_action_id")
    action = db.get(MonthlyPlanAction, info["plan_action_id"])
    assert action is not None
    assert action.status == "fulfilled" and action.fulfilled_at is not None
    assert not open_actions_for(db, user.id, isin=ISIN)


def _action(db, user_id, *, kind, broker, month="2026-10", account_id=None):
    row = MonthlyPlanAction(user_id=user_id, month=month, sleeve="core", kind=kind, isin=ISIN,
                            ticker="EUNL.DE", broker=broker, account_id=account_id)
    db.add(row)
    db.commit()
    return row


def test_buy_fulfils_only_a_buy_at_a_compatible_broker():
    db = _memory_db()
    user, pos = _user_with_position(db)
    rec = Recommendation(user_id=user.id, ticker="EUNL.DE", verdict="BUY", confidence=Decimal("0.6"),
                         payload_json=json.dumps({"candidate": {"isin": ISIN, "name": "World"}}),
                         approval_state="accepted", mode="discover")
    db.add(rec)
    db.commit()
    record_acceptance(db, rec)
    db.commit()
    sale = _action(db, user.id, kind="sale", broker="dkb")
    other_broker = _action(db, user.id, kind="order", broker="scalable")
    auto = _action(db, user.id, kind="savings_plan", broker="auto", month="2026-09")

    pos.quantity = Decimal("13")
    db.commit()
    assert len(detect_executions(db, user.id)) == 1

    db.refresh(rec)
    assert (execution_info(rec) or {}).get("plan_action_id") == auto.id
    for row, status in ((sale, "open"), (other_broker, "open"), (auto, "fulfilled")):
        db.refresh(row)
        assert row.status == status


def test_same_item_in_two_depots_stays_two_rows():
    db = _memory_db()
    user, _ = _user_with_position(db)
    _action(db, user.id, kind="sale", broker="dkb", account_id="a" * 36)
    _action(db, user.id, kind="sale", broker="dkb", account_id="b" * 36)
    assert db.query(MonthlyPlanAction).filter_by(user_id=user.id, kind="sale").count() == 2
