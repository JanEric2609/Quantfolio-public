"""Tests for lab/regime/history.py — the one-call-per-day regime ledger."""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import RegimeLabelHistory
from app.lab.regime.history import record_regime_label


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_writes_one_row_per_day_with_probabilities():
    db = _memory_db()
    row = record_regime_label(
        db, as_of=date(2026, 9, 29), label="bull", probs={"bull": 0.7, "sideways": 0.2, "bear": 0.1}, model="jump"
    )
    assert row is not None
    saved = db.query(RegimeLabelHistory).one()
    assert saved.as_of == date(2026, 9, 29)
    assert saved.label == "bull"
    assert saved.model == "jump"
    assert saved.probabilities_json == {"bull": 0.7, "sideways": 0.2, "bear": 0.1}
    assert saved.created_at is not None


def test_same_day_rerun_replaces_instead_of_duplicating():
    db = _memory_db()
    record_regime_label(db, as_of=date(2026, 9, 29), label="bull", probs={"bull": 0.6}, model="hmm")
    record_regime_label(db, as_of=date(2026, 9, 29), label="bear", probs={"bear": 0.8}, model="jump")
    rows = db.query(RegimeLabelHistory).all()
    assert len(rows) == 1
    assert (rows[0].label, rows[0].model) == ("bear", "jump")


def test_different_days_accumulate():
    db = _memory_db()
    for day, label in [(28, "bull"), (29, "bull"), (30, "sideways")]:
        record_regime_label(db, as_of=date(2026, 9, day), label=label, probs={label: 0.5}, model="jump")
    assert [r.label for r in db.query(RegimeLabelHistory).order_by(RegimeLabelHistory.as_of)] == [
        "bull",
        "bull",
        "sideways",
    ]


def test_as_of_is_unique_at_the_schema_level():
    db = _memory_db()
    db.add(RegimeLabelHistory(as_of=date(2026, 9, 1), label="bull", model="x"))
    db.commit()
    db.add(RegimeLabelHistory(as_of=date(2026, 9, 1), label="bear", model="x"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_write_failure_returns_none_and_leaves_session_usable():
    db = _memory_db()
    # A non-numeric probability cannot be stored: the ledger must swallow it.
    assert record_regime_label(db, as_of=date(2026, 9, 29), label="bull", probs={"bull": "n/a"}, model="jump") is None
    assert db.query(RegimeLabelHistory).count() == 0
    assert record_regime_label(db, as_of=date(2026, 9, 29), label="bull", probs={"bull": 0.5}, model="jump") is not None
