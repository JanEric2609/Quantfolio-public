"""The trust page's historical panel (ADR 0018 §1, plan A): built from stored records only."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification import build_history
from app.foundation.core.db import Base
from app.foundation.models.entities import EvidenceGateRun, FactorEvidenceCard
from app.foundation.settings import upsert_public_settings

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _card(region: str = "world", end: str = "2025-12-31") -> FactorEvidenceCard:
    yearly = {str(1963 + i): 0.03 + (0.02 if i % 3 == 0 else -0.01) for i in range(63)}
    return FactorEvidenceCard(
        run_id=f"run-{region}", strategy="value_momentum", region=region, months=756, passed=True,
        card_json={
            "strategy": "value_momentum", "label": "Value + momentum", "region": region, "citation": "Asness 2013",
            "months": 756, "start": "1963-07-31", "end": end, "long_short_t": 4.71, "long_only_annual": 0.034,
            "net_expected_annual": 0.009, "tracking_error_annual": 0.03, "worst_5y_excess": -0.08, "passed": True,
            "checks": [{"name": "t", "label": "Newey-West t >= 2", "value": 4.71, "passed": True}],
            "yearly_long_only": yearly,
        },
    )


def _gate() -> EvidenceGateRun:
    return EvidenceGateRun(
        region="world", satellite_unlocked=False, unlocked_by=[], n_trials=612,
        reason="No mined score's after-tax satellite passes the Deflated Sharpe test (best: dkb:ridge, DSR 0.41).",
        result_json={
            "families": [
                {"name": "pooled_long_short", "pbo": 0.12, "candidates": [
                    {"name": "ridge", "mined": True, "dsr": 0.999}, {"name": "gbm", "mined": True, "dsr": 0.998},
                ]},
                {"name": "satellite_after_tax", "pbo": 0.4, "candidates": [
                    {"name": "dkb:ridge", "mined": True, "dsr": 0.41, "sharpe_annual": 0.11, "months": 400},
                    {"name": "dkb:value_momentum", "mined": False, "dsr": 0.9},
                ]},
            ],
            "pooled_cards": [
                {"model": "ridge", "months": 709, "first_month": "1967-01-31", "last_month": "2025-12-31",
                 "ic_mean": 0.07, "ic_t": 16.0, "long_short_annual": 0.12, "long_only_annual": 0.035,
                 "recent": {"months": 120, "ic_mean": 0.03, "long_short_annual": 0.04}},
                {"model": "baseline", "months": 709},
            ],
            "pooled_computed_at": "2026-09-30T00:00:00+00:00",
        },
    )


def test_history_assembles_tilt_ranking_model_and_satellite_from_stored_runs():
    db = _memory_db()
    db.add_all([_card("world"), _card("europe", end="2024-06-30"), _gate()])
    db.commit()

    h = build_history(db, now=NOW)

    assert h["title"] == "Historical evidence (simulated, not live)"
    world, europe = h["tilt_cards"]
    assert world["gates_tilt"] and not europe["gates_tilt"]
    assert world["passed"] and world["long_short_t"] == pytest.approx(4.71)
    assert not world["stale"] and europe["stale"]  # data older than 12 months is flagged
    assert world["decay"]["full_period"] == pytest.approx(0.034)
    assert world["decay"]["after_haircut"] == pytest.approx(0.009)
    assert world["decay"]["last_10_years"] is not None
    fan = world["fan"]
    assert [p["p"] for p in fan["percentiles"]] == [5, 25, 50, 75, 95]
    values = [p["cumulative_excess"] for p in fan["percentiles"]]
    assert values == sorted(values) and 0.0 < fan["share_beat"] < 1.0

    rm = h["ranking_model"]
    assert rm["available"] and rm["status"] == "not_live"
    assert [m["model"] for m in rm["models"]] == ["ridge"]  # the baseline is not a model
    assert rm["models"][0]["ic_mean"] == 0.07 and rm["models"][0]["dsr"] == pytest.approx(0.999)
    assert rm["data_to"] == "2025-12-31" and rm["pbo"] == 0.12

    sat = h["satellite"]
    assert sat["available"] and not sat["unlocked"]
    assert sat["best"]["name"] == "dkb:ridge" and sat["best"]["dsr"] == pytest.approx(0.41)
    assert sat["n_candidates"] == 1  # the baseline is not a mined score

    assert h["n_trials"] == 612
    assert h["haircut"]["publication_decay"] == pytest.approx(0.58)
    assert "Nothing on this panel tests the Discover shortlist" in h["not_covered"]
    assert "Simulated. Not actual results." in h["disclosures"]
    assert h["tilt_review"]["status"] == "not_live"


def test_history_without_any_run_says_what_is_missing():
    db = _memory_db()

    h = build_history(db, now=NOW)

    assert h["tilt_cards"] == []
    assert h["ranking_model"]["available"] is False and h["ranking_model"]["models"] == []
    assert h["satellite"]["available"] is False and h["satellite"]["best"] is None
    assert h["n_trials"] >= 1


def test_the_tilt_review_rule_waits_for_twelve_live_months():
    db = _memory_db()
    upsert_public_settings(db, {"plan_tilt_isins": "IE00BP3QZ825"})

    review = build_history(db, now=NOW)["tilt_review"]

    assert review["status"] == "too_early" and "12 months" in review["note"]


def test_history_endpoint_serves_the_panel():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.foundation.models.entities import User
    from app.main import app

    db = _memory_db()
    user = User(username="h", password_hash="x")
    db.add_all([user, _card(), _gate()])
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        body = TestClient(app).get("/api/trust/history").json()
    finally:
        app.dependency_overrides.clear()

    assert body["tilt_cards"][0]["fan"]["years"] == 10
    assert body["satellite"]["unlocked"] is False
