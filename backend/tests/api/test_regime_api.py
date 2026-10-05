"""API tests for the regime endpoints (/api/data/regime/current, /history)."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.data_backbone.regime_store import RegimeStore


def _client():
    """Build a TestClient backed by an in-memory SQLite DB with regime_snapshots."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    # regime_snapshots is a TimescaleDB hypertable, not an ORM table.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP NOT NULL,
                label TEXT NOT NULL,
                score REAL NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}'
            )
        """))

    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)

    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user.id)
    return TestClient(app), maker


def test_regime_current_empty_returns_null_label():
    """With no snapshots, /regime/current returns label=None and crisis=False."""
    client, _maker = _client()
    resp = client.get("/api/data/regime/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] is None
    assert body["crisis"] is False
    assert body["probs"] == {}
    assert body["stale"] is False
    assert body["age_hours"] is None


def test_regime_current_returns_latest_snapshot():
    """/regime/current returns the latest snapshot with crisis flag and probs."""
    client, maker = _client()
    store = RegimeStore(maker())
    store.write_snapshot(
        ts=datetime(2026, 5, 1, tzinfo=timezone.utc),
        label="bull",
        score=0.8,
        source="jump",
        payload={"crisis": False, "probs": {"bull": 0.8, "sideways": 0.15, "bear": 0.05}},
    )
    # A later snapshot in crisis — should be the one returned.
    store.write_snapshot(
        ts=datetime(2026, 5, 2, tzinfo=timezone.utc),
        label="bear",
        score=0.7,
        source="jump",
        payload={"crisis": True, "probs": {"bull": 0.1, "sideways": 0.2, "bear": 0.7}},
    )

    resp = client.get("/api/data/regime/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "bear"
    assert body["crisis"] is True
    assert body["probs"]["bear"] == 0.7
    assert body["source"] == "jump"


def test_regime_current_stale_snapshot_flags_stale():
    """A snapshot older than the TTL is reported stale with its age in hours."""
    client, maker = _client()
    store = RegimeStore(maker())
    old_ts = datetime.now(timezone.utc) - timedelta(days=8)
    store.write_snapshot(
        ts=old_ts,
        label="sideways",
        score=0.1,
        source="jump",
        payload={"crisis": False},
    )

    resp = client.get("/api/data/regime/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stale"] is True
    assert body["age_hours"] > 144


def test_regime_current_fresh_snapshot_not_stale():
    """A recent snapshot reports stale=False."""
    client, maker = _client()
    store = RegimeStore(maker())
    store.write_snapshot(
        ts=datetime.now(timezone.utc),
        label="bull",
        score=0.8,
        source="jump",
        payload={"crisis": False},
    )

    resp = client.get("/api/data/regime/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stale"] is False
    assert 0 <= body["age_hours"] < 72


def test_regime_history_returns_ascending_list():
    """/regime/history returns all snapshots ascending by ts."""
    client, maker = _client()
    store = RegimeStore(maker())
    store.write_snapshot(
        ts=datetime(2026, 5, 1, tzinfo=timezone.utc),
        label="bull", score=0.8, source="jump", payload={"crisis": False},
    )
    store.write_snapshot(
        ts=datetime(2026, 5, 2, tzinfo=timezone.utc),
        label="bear", score=0.7, source="jump", payload={"crisis": True},
    )

    resp = client.get("/api/data/regime/history")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert [r["label"] for r in rows] == ["bull", "bear"]


def test_regime_history_empty_returns_empty_list():
    """/regime/history returns [] when there are no snapshots."""
    client, _maker = _client()
    resp = client.get("/api/data/regime/history")
    assert resp.status_code == 200
    assert resp.json() == []


def test_regime_current_ignores_other_models():
    """Only the jump model is the regime; an HMM row, even a later one, is not shown."""
    client, maker = _client()
    store = RegimeStore(maker())
    now = datetime.now(timezone.utc)
    store.write_snapshot(ts=now - timedelta(days=1), label="bull", score=0.0, source="jump", payload={})
    store.write_snapshot(ts=now, label="bear", score=0.9, source="hmm", payload={"crisis": True})
    body = client.get("/api/data/regime/current").json()
    assert body["label"] == "bull"
    assert body["source"] == "jump"
