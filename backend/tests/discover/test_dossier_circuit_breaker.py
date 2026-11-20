"""Tests for the orchestrator dossier loop: progress updates + LLM circuit breaker.

Guards against the "stuck at 'Generating dossiers...'" symptom where a dead
LLM endpoint cost a full HTTP timeout per shortlisted candidate and emitted
no per-candidate progress.
"""
from __future__ import annotations

import json

import pytest
import time
import uuid
from unittest.mock import patch
from sqlalchemy.orm import sessionmaker

from conftest import _memory_db

from app.foundation.models.entities import DiscoverCandidate, DiscoverRun
from app.decision.discover import orchestrator

@pytest.fixture(autouse=True)
def _no_earnings_lookup(monkeypatch):
    """The dossier loop asks Yahoo for each stock's next earnings date; keep it offline."""
    monkeypatch.setattr(
        "app.decision.discover.orchestrator.attach_earnings_calendar",
        lambda db, symbol, scores: scores,
    )



def _run_with_candidates(db, symbols: list[str]) -> tuple[DiscoverRun, list[DiscoverCandidate]]:
    run = DiscoverRun(
        id=str(uuid.uuid4()),
        user_id="user1",
        status="running",
        stage_json=json.dumps({}),
        params_json=json.dumps({}),
    )
    db.add(run)
    candidates = []
    for sym in symbols:
        cand = DiscoverCandidate(
            id=str(uuid.uuid4()),
            run_id=run.id,
            symbol=sym,
            source="test",
            status="shortlisted",
            scores_json=json.dumps({"composite": 0.7}),
            tradeable_json=json.dumps({"likely_tradeable": True}),
        )
        db.add(cand)
        candidates.append(cand)
    db.commit()
    return run, candidates


def _fake_dossier(generated_by: str, reason: str | None = None) -> dict:
    return {
        "dossier_id": str(uuid.uuid4()),
        "recommendation_id": str(uuid.uuid4()),
        "dossier": {},
        "generated_by": generated_by,
        "fallback_reason": reason,
    }


def _mock_session_local(db, monkeypatch):
    """Patch SessionLocal to return a sessionmaker that creates new sessions
    bound to the same engine as the test's db session."""
    engine = db.get_bind()
    test_sessionmaker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(
        "app.foundation.core.db.SessionLocal",
        test_sessionmaker,
    )


def test_circuit_breaker_opens_after_consecutive_llm_failures(monkeypatch):
    """After LLM_FAILURE_BREAKER consecutive llm_error fallbacks, the rest skip the LLM."""
    db = _memory_db()
    run, candidates = _run_with_candidates(db, ["AAA", "BBB", "CCC", "DDD", "EEE"])

    skip_flags: list[bool] = []

    def _fake_write_dossier(_db, candidate, _profile, prompt_config=None, skip_llm=False, config_id=None, track_record_cache=None):
        skip_flags.append(skip_llm)
        if skip_llm:
            return _fake_dossier("fallback", "llm_skipped: circuit breaker open")
        return _fake_dossier("fallback", "llm_error: connection refused")

    _mock_session_local(db, monkeypatch)
    monkeypatch.setattr(orchestrator, "write_dossier", _fake_write_dossier)

    count, _expected_return_by_symbol = orchestrator._generate_dossiers(
        db, run, candidates, {"user_id": "user1"},
        prompt_config=None,
        cancel_token=orchestrator.CancellationToken(db, run.id),
        started_at=time.time(),
    )

    # First LLM_FAILURE_BREAKER calls attempt the LLM, all later ones skip it.
    breaker = orchestrator.LLM_FAILURE_BREAKER
    assert skip_flags == [False] * breaker + [True] * (len(candidates) - breaker)
    # Every candidate still gets a (fallback) dossier.
    assert count == len(candidates)
    assert all(c.dossier_id for c in candidates)


def test_circuit_breaker_resets_on_llm_success(monkeypatch):
    """A successful LLM dossier resets the failure counter — no premature skip."""
    db = _memory_db()
    run, candidates = _run_with_candidates(db, ["AAA", "BBB", "CCC", "DDD"])

    outcomes = iter([
        _fake_dossier("fallback", "llm_error: timeout"),
        _fake_dossier("llm"),
        _fake_dossier("fallback", "llm_error: timeout"),
        _fake_dossier("llm"),
    ])
    skip_flags: list[bool] = []

    def _fake_write_dossier(_db, candidate, _profile, prompt_config=None, skip_llm=False, config_id=None, track_record_cache=None):
        skip_flags.append(skip_llm)
        return next(outcomes)

    _mock_session_local(db, monkeypatch)
    monkeypatch.setattr(orchestrator, "write_dossier", _fake_write_dossier)

    orchestrator._generate_dossiers(
        db, run, candidates, {"user_id": "user1"},
        prompt_config=None,
        cancel_token=orchestrator.CancellationToken(db, run.id),
        started_at=time.time(),
    )

    assert skip_flags == [False, False, False, False]


def test_dossier_loop_emits_per_candidate_progress(monkeypatch):
    """stage_json must advance per candidate (staleness detection depends on it)."""
    db = _memory_db()
    run, candidates = _run_with_candidates(db, ["AAA", "BBB", "CCC"])

    progress_seen: list[tuple[int | None, str | None]] = []

    def _fake_write_dossier(_db, candidate, _profile, prompt_config=None, skip_llm=False, config_id=None, track_record_cache=None):
        stage = json.loads(run.stage_json).get("discover", {})
        progress_seen.append((stage.get("processed_candidates"), stage.get("current_candidate")))
        return _fake_dossier("llm")

    _mock_session_local(db, monkeypatch)
    monkeypatch.setattr(orchestrator, "write_dossier", _fake_write_dossier)

    orchestrator._generate_dossiers(
        db, run, candidates, {"user_id": "user1"},
        prompt_config=None,
        cancel_token=orchestrator.CancellationToken(db, run.id),
        started_at=time.time(),
    )

    assert progress_seen == [(0, "AAA"), (1, "BBB"), (2, "CCC")]
    # Elapsed time is recorded so the debug endpoint can compute staleness.
    stage = json.loads(run.stage_json)["discover"]
    assert stage.get("elapsed_seconds") is not None
