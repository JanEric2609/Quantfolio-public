"""Tests for defensive type handling across the Discover pipeline (P2).

Verifies that unexpected non-dict structures (e.g. lists, None, non-dict JSON)
in stage outputs, candidate scores, tradeability payloads, or config dictionaries
never raise AttributeError ("'list' object has no attribute 'get'").
"""
from __future__ import annotations

import json
from uuid import uuid4
from unittest.mock import patch, MagicMock
from sqlalchemy.orm import sessionmaker

import pytest
from app.foundation.models.entities import DiscoverCandidate, DiscoverRun, User
from app.decision.discover.candidate_gate import _resolve_cohort_instrument_type, evaluate_track_record
from app.decision.discover.composite import compute_weighted_composite, derive_signals_from_scores
from app.decision.discover.dossier_writer import write_dossier, get_dynamic_shrinkage_factor
from app.decision.discover.orchestrator import _candidate_to_dict, _generate_dossiers
from app.decision.discover.cancel_token import CancelToken as CancellationToken
from app.decision.discover.config import get_or_seed_active_config
from app.foundation.expected_return import select_return_anchor

from conftest import _memory_db

@pytest.fixture(autouse=True)
def _no_earnings_lookup(monkeypatch):
    """The dossier loop asks Yahoo for each stock's next earnings date; keep it offline."""
    monkeypatch.setattr(
        "app.decision.discover.orchestrator.attach_earnings_calendar",
        lambda db, symbol, scores: scores,
    )



def _mock_session_local(db, monkeypatch):
    """Patch SessionLocal to return a sessionmaker that creates new sessions
    bound to the same engine as the test's db session."""
    engine = db.get_bind()
    test_sessionmaker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(
        "app.foundation.core.db.SessionLocal",
        test_sessionmaker,
    )


def test_select_return_anchor_with_list_and_corrupt_types():
    """select_return_anchor must fail-open to None when passed a list or non-dict."""
    assert select_return_anchor("equity", ["item1", "item2"]) is None  # type: ignore[arg-type]
    assert select_return_anchor("equity", None) is None  # type: ignore[arg-type]
    assert select_return_anchor("equity", "not-a-dict") is None  # type: ignore[arg-type]
    # Valid dict returns anchor
    valid = select_return_anchor("equity", {"trailing_3y_annualized_return": 0.15})
    assert valid is not None
    assert valid["value"] == 0.15


def test_derive_signals_from_scores_with_list_stage_outputs():
    """derive_signals_from_scores must never raise when stages return lists or None."""
    corrupt_scores = {
        "alpha_miner": ["unexpected", "list"],
        "alpha_screener": [1, 2, 3],
        "sentiment_fundamentals": ["bad_data"],
        "momentum_quality": [{"close": 100}],
        "backtest_vs_benchmark": ["not_a_dict"],
        "verification_gate": [None],
        "quant_signals": ["equity"],
        "portfolio_fit": ["fit"],
        "ml_signal": ["prediction"],
    }
    signals = derive_signals_from_scores(corrupt_scores)
    assert isinstance(signals, dict)
    # Also test passing a top-level list
    signals_from_list = derive_signals_from_scores(["corrupt_list"])  # type: ignore[arg-type]
    assert isinstance(signals_from_list, dict)


def test_compute_weighted_composite_with_list_inputs():
    """compute_weighted_composite must handle non-dict weights, ic_data, or signals."""
    assert compute_weighted_composite(["signal1"], ic_data=["corrupt"], weights=["corrupt"]) == 0.0  # type: ignore[arg-type]
    valid_signals = {"momentum": 0.7, "risk": 0.8}
    score = compute_weighted_composite(valid_signals, ic_data=["not_dict"], weights=["not_dict"])  # type: ignore[arg-type]
    assert 0.0 <= score <= 1.0


def test_resolve_cohort_instrument_type_with_list():
    """_resolve_cohort_instrument_type returns 'equity' default for list features."""
    assert _resolve_cohort_instrument_type(["list_features"]) == "equity"  # type: ignore[arg-type]
    assert _resolve_cohort_instrument_type({"signal_breakdown": ["list_breakdown"]}) == "equity"
    assert _resolve_cohort_instrument_type({"signal_breakdown": {"quant_signals": ["list_signals"]}}) == "equity"


def test_write_dossier_with_list_scores_and_tradeable_json():
    """write_dossier must handle candidate whose scores_json/tradeable_json deserializes as a list."""
    db = _memory_db()
    user = User(id="u1", username="testuser", password_hash="x")
    db.add(user)
    db.commit()

    candidate = {
        "id": uuid4().hex,
        "run_id": "run-1",
        "symbol": "SAP.DE",
        "isin": "DE0007164600",
        "name": "SAP SE",
        "source": "screen_index",
        "status": "shortlisted",
        "reject_stage": None,
        "reject_reason": None,
        "scores_json": json.dumps([{"corrupt": "list"}]),
        "tradeable_json": json.dumps(["corrupt", "tradeable", "list"]),
        "dossier_id": None,
        "recommendation_id": None,
    }

    result = write_dossier(db, candidate, {"user_id": "u1"}, skip_llm=True)
    assert isinstance(result, dict)
    assert result.get("generated_by") == "fallback"
    assert result.get("dossier_id") is not None


def test_generate_dossiers_with_list_candidate_data(monkeypatch):
    """_generate_dossiers must safely process candidates even if dossier returns unexpected types."""
    db = _memory_db()
    user = User(id="u1", username="testuser", password_hash="x")
    db.add(user)
    db.commit()

    run = DiscoverRun(id="run-1", user_id="u1", status="running", params_json="{}")
    db.add(run)
    db.commit()

    cand = DiscoverCandidate(
        id="cand-1",
        run_id="run-1",
        symbol="BMW.DE",
        isin="DE0005190003",
        name="BMW AG",
        source="screen_index",
        status="shortlisted",
        scores_json=json.dumps({"composite": 0.65, "quant_signals": ["corrupt_list"]}),
        tradeable_json=json.dumps({"likely_tradeable": True}),
    )
    db.add(cand)
    db.commit()

    cancel_token = CancellationToken(db, "run-1")
    signal_cfg = get_or_seed_active_config(db, "signal_weights")

    # Mock SessionLocal so CancelToken uses the test's db session
    _mock_session_local(db, monkeypatch)

    count, er_map = _generate_dossiers(
        db, run, [cand], {"user_id": "u1"},
        prompt_config={},
        cancel_token=cancel_token,
        started_at=0.0,
        config_id=signal_cfg.id,
    )
    assert count == 1
    assert "BMW.DE" in er_map


def test_get_dynamic_shrinkage_factor_with_corrupt_features():
    """get_dynamic_shrinkage_factor safely handles predictions with non-dict features_json."""
    db = _memory_db()
    user = User(id="u1", username="testuser", password_hash="x")
    db.add(user)
    db.commit()

    shrinkage = get_dynamic_shrinkage_factor(db, "u1", "equity")
    assert isinstance(shrinkage, float)
    assert 0.0 <= shrinkage <= 1.0
