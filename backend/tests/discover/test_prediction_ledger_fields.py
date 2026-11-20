"""Discover's own prediction-ledger rows carry an expected return and a thesis.

On prod every prediction a Discover run wrote (15 per run since #210) had
``expected_return`` and ``thesis`` NULL: the orchestrator read them off the
top level of ``write_dossier``'s result, where they never are. The only
filled rows came from the advisor cycle. The dynamic Mincer-Zarnowitz
shrinkage also read the cohort type from a key nothing writes.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from conftest import _memory_db

from app.decision.discover import orchestrator
from app.decision.discover.dossier_writer import (
    SHRINKAGE_BY_INSTRUMENT_TYPE,
    expected_return_over_trading_days,
    get_dynamic_shrinkage_factor,
)
from app.foundation.models.entities import DiscoverCandidate, DiscoverRun, DiscoveryPrediction

@pytest.fixture(autouse=True)
def _no_earnings_lookup(monkeypatch):
    """The dossier loop asks Yahoo for each stock's next earnings date; keep it offline."""
    monkeypatch.setattr(
        "app.decision.discover.orchestrator.attach_earnings_calendar",
        lambda db, symbol, scores: scores,
    )



def test_expected_return_is_restated_over_the_ledger_horizon_as_a_fraction():
    # 4.5 % a year prior + 2 % a year tilt, over 21 of 252 trading days.
    annual = {"prior_annual": 0.045, "tilt_annual": 0.02, "anchor_kind": "trailing_3y_cagr"}

    assert expected_return_over_trading_days(annual, 21) == pytest.approx(0.065 * 21 / 252)


def test_momentum_tilt_is_capped_at_one_month_like_the_dossier_estimate():
    annual = {"prior_annual": 0.06, "tilt_annual": 0.12, "anchor_kind": "momentum_12_1m"}

    over_quarter = expected_return_over_trading_days(annual, 63)

    assert over_quarter == pytest.approx(0.06 * 63 / 252 + 0.12 / 12)


def test_no_anchor_means_no_estimate():
    assert expected_return_over_trading_days(None, 21) is None
    assert expected_return_over_trading_days({"prior_annual": None, "tilt_annual": 0.0}, 21) is None


def test_generate_dossiers_threads_the_nested_estimate_and_thesis(monkeypatch):
    db = _memory_db()
    run = DiscoverRun(id=str(uuid.uuid4()), user_id="user1", status="running", stage_json="{}", params_json="{}")
    db.add(run)
    cand = DiscoverCandidate(
        id=str(uuid.uuid4()), run_id=run.id, symbol="MS", source="screen_index", status="shortlisted",
        scores_json=json.dumps({"composite": 0.63}), tradeable_json=json.dumps({"likely_tradeable": True}),
    )
    db.add(cand)
    db.commit()

    def fake_write_dossier(*_args, **_kwargs):
        return {
            "dossier_id": str(uuid.uuid4()),
            "recommendation_id": str(uuid.uuid4()),
            # The shape write_dossier really returns: the dossier is nested.
            "dossier": {"expected_return": 3.4, "thesis": "Investment-banking fees recover."},
            "generated_by": "llm",
            "fallback_reason": None,
            "annual_estimate": {"prior_annual": 0.07, "tilt_annual": -0.002, "anchor_kind": "trailing_3y_cagr"},
        }

    engine = db.get_bind()
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", sessionmaker(bind=engine, autoflush=False))
    monkeypatch.setattr(orchestrator, "write_dossier", fake_write_dossier)

    _count, fields = orchestrator._generate_dossiers(
        db, run, [cand], {"user_id": "user1"},
        prompt_config=None, cancel_token=orchestrator.CancellationToken(db, run.id), started_at=time.time(),
    )

    assert fields["MS"]["thesis"] == "Investment-banking fees recover."
    assert fields["MS"]["expected_return"] == pytest.approx(0.068 * orchestrator.DEFAULT_HORIZON_DAYS / 252)


def test_dynamic_shrinkage_reads_the_cohort_type_where_the_ledger_stores_it():
    db = _memory_db()
    now = datetime.now(UTC)
    for i in range(25):
        expected = 0.002 * i
        db.add(DiscoveryPrediction(
            user_id="user1", run_id="run1", symbol=f"S{i}", predicted_at=now - timedelta(days=40),
            horizon_days=21, resolve_at=now - timedelta(days=10), direction="buy",
            expected_return=expected, realised_return=0.5 * expected + (0.001 if i % 2 else -0.001),
            outcome_status="resolved",
            features_json={"signal_breakdown": {"quant_signals": {"instrument_type": "equity"}}},
        ))
    db.commit()

    factor = get_dynamic_shrinkage_factor(db, "user1", "equity")

    assert factor != SHRINKAGE_BY_INSTRUMENT_TYPE["equity"]
    assert factor == pytest.approx(0.5, abs=0.05)


def test_dynamic_shrinkage_ignores_the_advisor_cycle_rows():
    # The advisor writes Monte Carlo p50s to the same ledger, without
    # quant_signals; they must not calibrate Discover's estimator.
    db = _memory_db()
    now = datetime.now(UTC)
    for i in range(25):
        db.add(DiscoveryPrediction(
            user_id="user1", run_id="advisor-cycle", symbol=f"S{i}", predicted_at=now - timedelta(days=40),
            horizon_days=21, resolve_at=now - timedelta(days=10), direction="buy",
            expected_return=0.002 * i, realised_return=0.001 * i, outcome_status="resolved",
            features_json={"signal_breakdown": {"risk_envelope": {}, "traded": True}},
        ))
    db.commit()

    assert get_dynamic_shrinkage_factor(db, "user1", "equity") == SHRINKAGE_BY_INSTRUMENT_TYPE["equity"]
