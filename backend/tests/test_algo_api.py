"""The algo overview: read-only, signed-in users only, nothing funded (ADR 0029)."""
from fastapi.testclient import TestClient

from app.lab.algo_lab import cards


def test_cards_respect_the_family_cap_and_are_all_risk_overlays():
    assert len(cards.CARDS) <= cards.MAX_ARMS
    assert {c["verdict_class"] for c in cards.CARDS} == {"risk overlay"}
    assert len({c["id"] for c in cards.CARDS}) == len(cards.CARDS)


def test_overview_needs_a_session_and_funds_nothing():
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/algo/overview").status_code in (401, 403)
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/api/algo")
                and (getattr(r, "methods", set()) - {"GET", "HEAD"})]


def test_overview_payload(monkeypatch):
    from app.foundation.auth import current_user
    from app.main import app

    app.dependency_overrides[current_user] = lambda: object()
    try:
        body = TestClient(app).get("/api/algo/overview").json()
    finally:
        app.dependency_overrides.pop(current_user, None)
    assert body["paper_only"] is True and body["funded_candidates"] == 0
    assert body["years_to_detect"][0]["sharpe"] == 0.5 and body["years_to_detect"][0]["years"] > 20
    assert any("#0005" in n["quantpedia"] for n in body["not_carded"])
