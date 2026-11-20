from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import learning_ledger as ll
from app.foundation.core.db import Base
from app.foundation.models.entities import LearningPurchase


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _Pos:
    def __init__(self, isin, qty, source="trading212"):
        self.isin, self.quantity, self.source = isin, Decimal(str(qty)), source


def _user(db):
    from app.foundation.models.entities import User
    u = User(id="u", username="u", password_hash="x")
    db.add(u)
    db.commit()
    return u.id


LINE = {"amount_eur": 20.0, "pick": {"isin": "US0000000001", "ticker": "AAA", "name": "Aaa"}}


def test_record_planned_once_per_month_and_skips_empty_line():
    db = _memory_db()
    uid = _user(db)
    with patch.object(ll, "live_positions", return_value=[_Pos("US0000000001", 2)]), \
         patch.object(ll, "latest_cached_close", return_value=10.0):
        assert ll.record_planned(db, uid, {"amount_eur": 0, "pick": None}, date(2026, 11, 15)) is None
        a = ll.record_planned(db, uid, LINE, date(2026, 11, 15))
        db.commit()
        b = ll.record_planned(db, uid, {**LINE, "amount_eur": 5}, date(2026, 11, 20))
    assert a is not None and b is not None and a.id == b.id
    assert a.amount_eur == 20.0 and a.baseline_quantity == 2.0 and a.month == date(2026, 11, 1)


def test_fill_detected_from_position_growth_other_sources_ignored():
    db = _memory_db()
    uid = _user(db)
    with patch.object(ll, "live_positions", return_value=[_Pos("US0000000001", 1)]), \
         patch.object(ll, "latest_cached_close", return_value=10.0):
        ll.record_planned(db, uid, LINE, date(2026, 11, 1))
        db.commit()
    with patch.object(ll, "live_positions", return_value=[_Pos("US0000000001", 1), _Pos("US0000000001", 5, "scalable")]):
        assert ll.detect_fills(db, uid) == []
    with patch.object(ll, "live_positions", return_value=[_Pos("US0000000001", 1.4)]):
        assert len(ll.detect_fills(db, uid)) == 1
    row = db.query(LearningPurchase).one()
    assert row.status == "filled" and abs(row.fill_quantity - 0.4) < 1e-9


def test_unfilled_month_is_skipped_after_watch_window():
    db = _memory_db()
    uid = _user(db)
    with patch.object(ll, "live_positions", return_value=[]), patch.object(ll, "latest_cached_close", return_value=10.0):
        ll.record_planned(db, uid, LINE, date(2026, 11, 1))
        db.commit()
        ll.detect_fills(db, uid, now=datetime.now(UTC) + timedelta(days=ll.WATCH_DAYS + 1))
    assert db.query(LearningPurchase).one().status == "skipped"


def test_results_compare_real_with_shadow_and_implied_fill_price():
    db = _memory_db()
    uid = _user(db)
    prices = {"AAA": 10.0, "EUNL.DE": 100.0}
    with patch.object(ll, "live_positions", return_value=[]), \
         patch.object(ll, "latest_cached_close", side_effect=lambda _db, t: prices[t]):
        ll.record_planned(db, uid, LINE, date(2026, 11, 1))
        db.commit()
    row = db.query(LearningPurchase).one()
    row.status, row.fill_quantity = "filled", 2.0  # 20 EUR for 2 units: implied 10, paper 10
    db.commit()
    prices.update({"AAA": 12.0, "EUNL.DE": 105.0})
    with patch.object(ll, "latest_cached_close", side_effect=lambda _db, t: prices[t]):
        out = ll.results(db, uid)
    p = out["purchases"][0]
    assert out["count_filled"] == 1 and out["invested_eur"] == 20.0
    assert p["fill_price_is_implied"] is True and p["fill_vs_paper_pct"] == 0.0
    assert out["real_value_eur"] == 24.0 and out["shadow_value_eur"] == 21.0
    assert out["note"] == "A learning expense you chose. It is not evidence that the picks work."


def test_results_stay_out_of_lessons_prompts_trial_ledger_and_ladder_evidence():
    app = Path(__file__).parents[1] / "app"
    allowed = {"decision/learning_ledger.py", "decision/monthly_plan_store.py", "interface/api/graduation.py",
               "foundation/models/entities/ladder.py", "foundation/models/entities/__init__.py"}
    offenders = []
    for path in app.rglob("*.py"):
        rel = path.relative_to(app).as_posix()
        text = path.read_text(encoding="utf-8")
        if ("LearningPurchase" in text or "learning_ledger" in text or "learning_purchases" in text) and rel not in allowed:
            offenders.append(rel)
    assert offenders == []
