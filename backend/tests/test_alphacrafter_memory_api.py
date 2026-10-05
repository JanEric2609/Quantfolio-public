"""API tests for AlphaCrafter shared memory inspection endpoints.

Includes user-scoping (IDOR) regression tests: jobs and dossiers belong to the
user who created them; legacy rows (NULL user_id) are admin-visible only.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import AlphacrafterJobRun, RecommendationDossier, User
from app.foundation.auth import current_user


def _client(role: str = "user"):
    """Build a TestClient backed by an in-memory SQLite DB.

    Returns (client, maker, user_id, app) — override ``current_user`` on the
    app to act as a different user.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    user = User(username="jan", password_hash="hash", role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id

    app = create_app()

    def test_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[current_user] = lambda: maker().get(User, user_id)

    return TestClient(app), maker, user_id, app


def _add_user(maker, username: str, role: str = "user") -> str:
    db = maker()
    user = User(username=username, password_hash="hash", role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id
    db.close()
    return user_id


def _add_job(maker, *, user_id: str | None, shared_memory=None, status: str = "completed") -> str:
    db = maker()
    job_id = str(uuid4())
    db.add(AlphacrafterJobRun(id=job_id, user_id=user_id, status=status, shared_memory=shared_memory))
    db.commit()
    db.close()
    return job_id


_SHARED_MEM = {
    "market_state": {"universe": ["AAPL", "MSFT"], "regime_label": "bull"},
    "factor_states": [],
    "agent_history": [
        {"agent": "miner", "action": "start", "timestamp": "2026-01-01T00:00:00+00:00"}
    ],
}


def test_get_memory_nonexistent_job_returns_404():
    """GET /jobs/{id}/memory returns 404 for a job that doesn't exist."""
    client, _maker, _uid, _app = _client()
    resp = client.get(f"/api/alphacrafter/jobs/{uuid4()}/memory")
    assert resp.status_code == 404
    body = resp.json()
    # App wraps errors in {"error": {"code": ..., "message": ...}}
    assert "not found" in body.get("error", {}).get("message", "").lower()


def test_get_timeline_nonexistent_job_returns_empty():
    """GET /jobs/{id}/timeline returns empty list for a job that doesn't exist."""
    client, _maker, _uid, _app = _client()
    resp = client.get(f"/api/alphacrafter/jobs/{uuid4()}/timeline")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_memory_completed_job_returns_snapshot():
    """GET /jobs/{id}/memory returns the SharedMemoryH snapshot for a completed job."""
    client, maker, user_id, _app = _client()
    job_id = _add_job(maker, user_id=user_id, shared_memory=_SHARED_MEM)

    resp = client.get(f"/api/alphacrafter/jobs/{job_id}/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["market_state"]["universe"] == ["AAPL", "MSFT"]
    assert body["market_state"]["regime_label"] == "bull"
    assert "agent_history" in body


def test_get_timeline_completed_job_returns_history():
    """GET /jobs/{id}/timeline returns the agent_history list."""
    client, maker, user_id, _app = _client()
    shared_mem = {
        "market_state": {"universe": [], "regime_label": None},
        "factor_states": [],
        "agent_history": [
            {"agent": "miner", "action": "start", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"agent": "screener", "action": "evaluate", "timestamp": "2026-01-01T00:01:00+00:00"},
        ],
    }
    job_id = _add_job(maker, user_id=user_id, shared_memory=shared_mem)

    resp = client.get(f"/api/alphacrafter/jobs/{job_id}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["agent"] == "miner"
    assert body[1]["agent"] == "screener"


def test_get_memory_job_without_shared_memory_returns_404():
    """GET /jobs/{id}/memory returns 404 when shared_memory is None."""
    client, maker, user_id, _app = _client()
    job_id = _add_job(maker, user_id=user_id, shared_memory=None)

    resp = client.get(f"/api/alphacrafter/jobs/{job_id}/memory")
    assert resp.status_code == 404


def test_get_timeline_job_without_shared_memory_returns_empty():
    """GET /jobs/{id}/timeline returns empty list when shared_memory is None."""
    client, maker, user_id, _app = _client()
    job_id = _add_job(maker, user_id=user_id, shared_memory=None)

    resp = client.get(f"/api/alphacrafter/jobs/{job_id}/timeline")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# User-scoping (IDOR) regression tests — issue #127
# ---------------------------------------------------------------------------

def test_other_users_job_is_hidden():
    """A user cannot read another user's job status, memory, or timeline."""
    client, maker, _user_id, _app = _client()
    other_id = _add_user(maker, "other")
    job_id = _add_job(maker, user_id=other_id, shared_memory=_SHARED_MEM)

    assert client.get(f"/api/alphacrafter/jobs/{job_id}/status").status_code == 404
    assert client.get(f"/api/alphacrafter/jobs/{job_id}/memory").status_code == 404
    timeline = client.get(f"/api/alphacrafter/jobs/{job_id}/timeline")
    assert timeline.status_code == 200
    assert timeline.json() == []


def test_legacy_job_hidden_from_regular_user_visible_to_admin():
    """Jobs with NULL user_id (pre-scoping legacy) are admin-visible only."""
    client, maker, _user_id, _app = _client()
    job_id = _add_job(maker, user_id=None, shared_memory=_SHARED_MEM)
    assert client.get(f"/api/alphacrafter/jobs/{job_id}/memory").status_code == 404

    admin_client, admin_maker, _admin_id, _admin_app = _client(role="admin")
    admin_job_id = _add_job(admin_maker, user_id=None, shared_memory=_SHARED_MEM)
    assert admin_client.get(f"/api/alphacrafter/jobs/{admin_job_id}/memory").status_code == 200


def test_own_job_status_visible():
    """A user can read their own job status."""
    client, maker, user_id, _app = _client()
    job_id = _add_job(maker, user_id=user_id, status="running")
    resp = client.get(f"/api/alphacrafter/jobs/{job_id}/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


def _add_dossier(maker, *, user_id: str | None, symbol: str = "AAPL") -> str:
    db = maker()
    dossier_id = str(uuid4())
    db.add(
        RecommendationDossier(
            id=dossier_id,
            user_id=user_id,
            conviction=0.7,
            dossier_json=f'{{"symbol": "{symbol}", "recommendation": "BUY {symbol}"}}',
        )
    )
    db.commit()
    db.close()
    return dossier_id


def test_dossiers_are_user_scoped():
    """List returns only own dossiers; another user's dossier fetch is 404."""
    client, maker, user_id, _app = _client()
    other_id = _add_user(maker, "other")
    own = _add_dossier(maker, user_id=user_id)
    theirs = _add_dossier(maker, user_id=other_id)
    legacy = _add_dossier(maker, user_id=None)

    listed = client.get("/api/alphacrafter/dossiers")
    assert listed.status_code == 200
    ids = {d["id"] for d in listed.json()}
    assert own in ids
    assert theirs not in ids
    assert legacy not in ids

    assert client.get(f"/api/alphacrafter/dossiers/{own}").status_code == 200
    assert client.get(f"/api/alphacrafter/dossiers/{theirs}").status_code == 404
