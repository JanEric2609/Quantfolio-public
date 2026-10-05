"""GET /api/portfolio/advisor/pending: what Decide lists for Accept / Reject / Snooze."""
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import (
    Recommendation,
    RecommendationDossier,
    RecommendationReview,
    User,
)
from app.main import create_app


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _client(db, user):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def _rec(db, user, ticker, state="approved_candidate", *, age_days=1, payload=None, **fields):
    row = Recommendation(
        user_id=user.id,
        ticker=ticker,
        verdict=fields.pop("verdict", "WATCH"),
        confidence=fields.pop("confidence", 0.5),
        payload_json=json.dumps(payload or {}),
        approval_state=state,
        mode=fields.pop("mode", "discover"),
        created_at=datetime.now(UTC) - timedelta(days=age_days),
        **fields,
    )
    db.add(row)
    db.commit()
    return row


def _setup():
    db = _memory_db()
    me = User(username="jan", password_hash="hash")
    other = User(username="eve", password_hash="hash")
    db.add_all([me, other])
    db.commit()
    return db, me, other


def test_lists_only_pending_states_of_the_caller():
    db, me, other = _setup()
    _rec(db, me, "AAA", "approved_candidate")
    _rec(db, me, "BBB", "needs_review")
    _rec(db, me, "DRAFT", "draft")  # withheld as unproven: never a decision
    _rec(db, me, "DONE", "accepted")
    _rec(db, me, "NO", "rejected")
    _rec(db, me, "RUN", "complete", mode="portfolio_advisor")
    _rec(db, other, "THEIRS", "approved_candidate")

    body = _client(db, me).get("/api/portfolio/advisor/pending").json()

    assert {item["ticker"] for item in body["items"]} == {"AAA", "BBB"}
    assert body["total"] == 2


def test_newest_first_and_total_counts_past_the_limit():
    db, me, _ = _setup()
    for index in range(5):
        _rec(db, me, f"T{index}", age_days=index + 1)

    body = _client(db, me).get("/api/portfolio/advisor/pending?limit=2").json()

    assert [item["ticker"] for item in body["items"]] == ["T0", "T1"]
    assert body["total"] == 5


def test_expired_and_stale_recommendations_are_left_out():
    db, me, _ = _setup()
    _rec(db, me, "LIVE", recommendation_expiry=datetime.now(UTC) + timedelta(days=3))
    _rec(db, me, "EXPIRED", recommendation_expiry=datetime.now(UTC) - timedelta(days=1))
    _rec(db, me, "OLD", age_days=45)

    client = _client(db, me)
    assert [i["ticker"] for i in client.get("/api/portfolio/advisor/pending").json()["items"]] == ["LIVE"]
    # A wider window brings the old one back.
    wide = client.get("/api/portfolio/advisor/pending?max_age_days=90").json()
    assert {i["ticker"] for i in wide["items"]} == {"LIVE", "OLD"}


def test_snoozed_recommendation_resurfaces_after_the_snooze_period():
    db, me, _ = _setup()
    fresh = _rec(db, me, "FRESH", "snoozed")
    due = _rec(db, me, "DUE", "snoozed")
    for rec, days_ago in ((fresh, 1), (due, 8)):
        db.add(
            RecommendationReview(
                recommendation_id=rec.id,
                user_id=me.id,
                from_state="approved_candidate",
                to_state="snoozed",
                created_at=datetime.now(UTC) - timedelta(days=days_ago),
            )
        )
    db.commit()

    body = _client(db, me).get("/api/portfolio/advisor/pending").json()

    assert [item["ticker"] for item in body["items"]] == ["DUE"]
    assert body["items"][0]["resurfaced"] is True


def test_item_carries_summary_name_and_a_normalised_confidence():
    db, me, _ = _setup()
    dossier = RecommendationDossier(
        user_id=me.id,
        conviction=0.6,
        dossier_json=json.dumps({"thesis": "Quality compounder with a cheap multiple."}),
    )
    db.add(dossier)
    db.commit()
    _rec(
        db, me, "DISC", confidence=0.6, verdict="WATCH",
        payload={"dossier_id": dossier.id, "candidate": {"name": "Discover Corp"}},
    )
    _rec(db, me, "V2", "needs_review", confidence=72, mode="long_term", payload={"thesis": "Direct thesis."})

    items = {i["ticker"]: i for i in _client(db, me).get("/api/portfolio/advisor/pending").json()["items"]}

    assert items["DISC"]["summary"] == "Quality compounder with a cheap multiple."
    assert items["DISC"]["name"] == "Discover Corp"
    assert items["DISC"]["confidence"] == 0.6
    assert items["V2"]["summary"] == "Direct thesis."
    assert items["V2"]["confidence"] == 0.72


def test_feedback_moves_an_item_out_of_the_pending_list():
    db, me, _ = _setup()
    rec = _rec(db, me, "AAA")
    client = _client(db, me)

    assert client.get("/api/portfolio/advisor/pending").json()["total"] == 1
    assert client.post(f"/api/portfolio/advisor/feedback/{rec.id}?action=accepted").status_code == 200
    assert client.get("/api/portfolio/advisor/pending").json() == {"items": [], "total": 0}
