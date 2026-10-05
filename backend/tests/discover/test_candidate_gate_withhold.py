"""Tests for Track B2 — auto-withhold wiring from candidate_gate into
Recommendation.approval_state, and its surfacing on CandidateResponse."""
from __future__ import annotations

import json

from conftest import _memory_db

from app.foundation.models.entities import Recommendation
from app.decision.discover.dossier_writer import write_dossier


def _candidate() -> dict:
    return {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "isin": "US0378331005",
        "source": "screen_index",
        "scores_json": json.dumps({"composite": 0.85}),
        "tradeable_json": json.dumps({"teilfreistellung_class": "aktien", "domicile": "US"}),
    }


def _write(db, monkeypatch, gate_result: dict):
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._local_llm_sync",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: None,
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_sentiment",
        lambda _symbol, _db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"},
    )
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer.evaluate_track_record",
        lambda *_a, **_kw: gate_result,
    )
    return write_dossier(db, _candidate(), {"user_id": "user1"}, skip_llm=True, config_id="cfg-1")


def test_unproven_cohort_withholds_recommendation(monkeypatch):
    db = _memory_db()
    result = _write(db, monkeypatch, {"status": "unproven", "n": 40, "t_stat": 0.1, "hit_rate": 0.4})

    rec = db.get(Recommendation, result["recommendation_id"])
    assert rec.approval_state == "draft"
    payload = json.loads(rec.payload_json)
    assert payload["track_record_gate"]["status"] == "unproven"


def test_proven_cohort_approves_recommendation(monkeypatch):
    db = _memory_db()
    result = _write(db, monkeypatch, {"status": "proven", "n": 40, "t_stat": 4.2, "hit_rate": 0.7})

    rec = db.get(Recommendation, result["recommendation_id"])
    assert rec.approval_state == "approved_candidate"


def test_insufficient_data_fails_open_and_approves(monkeypatch):
    db = _memory_db()
    result = _write(
        db, monkeypatch,
        {"status": "insufficient_data", "n": 5, "min_n": 30, "t_stat": None, "hit_rate": None},
    )

    rec = db.get(Recommendation, result["recommendation_id"])
    assert rec.approval_state == "approved_candidate"


def test_verdict_follows_the_track_record_not_the_score(monkeypatch):
    """A 0.85 composite used to mean BUY. The score is a ranking and its
    level moves with every weight change, so only a proven cohort buys."""
    for status, verdict in (
        ("proven", "BUY"),
        ("unproven", "WATCH"),
        ("insufficient_data", "WATCH"),
    ):
        db = _memory_db()
        result = _write(db, monkeypatch, {"status": status, "n": 40, "t_stat": 1.0, "hit_rate": 0.5})
        rec = db.get(Recommendation, result["recommendation_id"])
        assert rec.verdict == verdict, status
