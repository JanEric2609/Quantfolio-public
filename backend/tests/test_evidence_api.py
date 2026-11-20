"""Tests for /api/evidence — read-only surfacing of the global trial ledger
(ADR 0015, Phase 1)."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import User
from app.foundation.quant_metrics import DEFAULT_N_TRIALS_FLOOR, record_trial


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db):
    user = User(username="evidence-user", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _api_client(db, user):
    from fastapi.testclient import TestClient

    from app.foundation.core.db import get_db
    from app.main import app
    from app.foundation.auth import current_user

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def _clear_overrides():
    from app.main import app

    app.dependency_overrides.clear()


def test_n_trials_summary_on_empty_ledger():
    db = _memory_db()
    user = _user(db)
    client = _api_client(db, user)
    try:
        resp = client.get("/api/evidence/n-trials")
        assert resp.status_code == 200
        body = resp.json()
        assert body["resolved"] == DEFAULT_N_TRIALS_FLOOR
        assert body["floor"] == DEFAULT_N_TRIALS_FLOOR
        assert body["raw_count"] == 0
        assert body["effective"] == 1
        assert body["by_context"] == {}
        assert body["hurdle_curve"][0] == {"n_trials": 1, "sharpe_hurdle": 0.0}
    finally:
        _clear_overrides()


def test_n_trials_summary_reflects_recorded_trials():
    db = _memory_db()
    user = _user(db)
    for i in range(3):
        record_trial(db, context="alphacrafter_tuning", trial_key=f"k{i}")
    for i in range(2):
        record_trial(db, context="discover_candidate", trial_key=f"k{i}")
    db.commit()

    client = _api_client(db, user)
    try:
        resp = client.get("/api/evidence/n-trials")
        assert resp.status_code == 200
        body = resp.json()
        assert body["raw_count"] == 5
        assert body["resolved"] == 5
        assert body["by_context"] == {"alphacrafter_tuning": 3, "discover_candidate": 2}
    finally:
        _clear_overrides()


def test_trial_ledger_entries_lists_recent_rows():
    db = _memory_db()
    user = _user(db)
    record_trial(db, context="alphacrafter_tuning", trial_key="a", metadata={"x": 1})
    record_trial(db, context="discover_candidate", trial_key="b")
    db.commit()

    client = _api_client(db, user)
    try:
        resp = client.get("/api/evidence/trial-ledger")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 2
        assert {r["context"] for r in rows} == {"alphacrafter_tuning", "discover_candidate"}
        by_key = {r["trial_key"]: r for r in rows}
        assert by_key["a"]["metadata"] == {"x": 1}
    finally:
        _clear_overrides()


def test_trial_ledger_entries_filters_by_context():
    db = _memory_db()
    user = _user(db)
    record_trial(db, context="alphacrafter_tuning", trial_key="a")
    record_trial(db, context="discover_candidate", trial_key="b")
    db.commit()

    client = _api_client(db, user)
    try:
        resp = client.get("/api/evidence/trial-ledger", params={"context": "discover_candidate"})
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["context"] == "discover_candidate"
    finally:
        _clear_overrides()


def test_evidence_endpoints_require_auth():
    db = _memory_db()
    from app.foundation.core.db import get_db
    from app.main import app
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    try:
        resp = client.get("/api/evidence/n-trials")
        assert resp.status_code in (401, 403)
    finally:
        _clear_overrides()
