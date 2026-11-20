"""The regime every surface shows is the jump model's stored state."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_backbone.regime_store import RegimeStore
from app.lab.regime.gate import get_regime_label
from app.lab.regime.macro_snapshot import compute_regime_snapshot, get_or_refresh_regime


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS regime_snapshots (ts TIMESTAMP, label TEXT, score REAL, source TEXT, payload_json TEXT)"
    ))
    return db


def _write(db, days_ago, label, source="jump", vix=15.3):
    RegimeStore(db).write_snapshot(
        datetime.now(UTC) - timedelta(days=days_ago), label, 0.97, source,
        {"crisis": False, "feature_ts": "2026-10-02T00:00:00+00:00", "features": {"vix": vix, "credit_spread": 1.49,
                                                                                  "drawdown": -0.05}},
    )


def test_no_snapshot_is_unknown_not_low_vol():
    snap = get_or_refresh_regime(_memory_db())
    assert snap["label"] == "unknown" and snap["confidence"] is None and snap["available"] is False
    assert get_regime_label(snap) == "neutral"


def test_latest_jump_state_with_the_day_it_began():
    db = _memory_db()
    _write(db, 5, "sideways")
    _write(db, 3, "bull")
    _write(db, 2, "bull")
    _write(db, 1, "sideways", source="hmm")  # the HMM fallback is ignored
    _write(db, 0, "bull")
    snap = compute_regime_snapshot(db)
    assert snap["label"] == "bull" and snap["model"] == "jump" and snap["available"]
    assert snap["confidence"] is None  # a state, not a probability
    assert snap["vix"] == 15.3 and snap["credit_spread"] == 1.49
    assert snap["state_since"] == (datetime.now(UTC) - timedelta(days=3)).date().isoformat()


def test_a_stale_classification_reads_unknown():
    db = _memory_db()
    _write(db, 10, "bear")
    snap = compute_regime_snapshot(db)
    assert snap["label"] == "unknown" and snap.get("stale") is True
