"""Tests for discover/candidate_gate.py: does Discover have a proven edge?

The gate judges one observation per 30-day block of prediction dates (the
mean excess of the block's picks) with the anytime-valid sequential t-test
of Wang & Ramdas (arXiv 2310.03722, Thm 4.11), across all signal-weight
versions of an instrument type.
"""
import math
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover.candidate_gate import (
    EVIDENCE_ALPHA,
    evaluate_track_record,
    one_sided_t_e_value,
)
from app.foundation.core.db import Base
from app.foundation.models.entities import DiscoveryPrediction, User

START = datetime(2026, 1, 5, tzinfo=UTC)
NOW = datetime(2027, 6, 30, tzinfo=UTC)
# A steady ~1% a month over 12 blocks. With the mixture's c = 2 even a
# perfectly consistent record cannot reach 1/alpha in fewer than 9 blocks.
STEADY = [0.012, 0.009, 0.011, 0.010, 0.008, 0.013, 0.011, 0.010, 0.012, 0.009, 0.010, 0.011]


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db):
    user = User(username="candidate-gate-user", password_hash="x")
    db.add(user)
    db.commit()
    return user


def _pick(
    db,
    user_id,
    *,
    day: int,
    excess: float | None,
    hit: bool | None = True,
    instrument_type: str = "equity",
    config_id: str | None = "cfg-1",
    status: str = "resolved",
    symbol: str = "AAPL",
):
    predicted = START + timedelta(days=day)
    db.add(DiscoveryPrediction(
        user_id=user_id,
        run_id=f"run-{day}",
        symbol=symbol,
        predicted_at=predicted,
        horizon_days=21,
        resolve_at=predicted + timedelta(days=30),
        config_id=config_id,
        outcome_status=status,
        realised_return=None if excess is None else excess + 0.01,
        excess_return=excess,
        score_json={"hit": hit} if hit is not None else {},
        features_json={"signal_breakdown": {"quant_signals": {"instrument_type": instrument_type}}},
    ))


def _monthly(db, user_id, means, **kwargs):
    """Three picks per 30-day block, centred on each block mean."""
    for block, mean in enumerate(means):
        for j, offset in enumerate((-0.002, 0.0, 0.002)):
            _pick(db, user_id, day=block * 30 + j, excess=mean + offset, symbol=f"S{block}{j}", **kwargs)
    db.commit()


# --- the e-value --------------------------------------------------------------


def test_e_value_matches_the_closed_form():
    x = [0.02, 0.01, 0.03, -0.005]
    n, s, v, c = len(x), sum(x), sum(t * t for t in x), 2.0
    expected = 2 * math.sqrt(c * c / (n + c * c)) * ((1 - s * s / ((n + c * c) * v)) ** (-n / 2) - 1)
    assert one_sided_t_e_value(x) == pytest.approx(expected, rel=1e-9)


def test_e_value_gives_no_evidence_for_a_negative_or_empty_mean():
    assert one_sided_t_e_value([]) == 0.0
    assert one_sided_t_e_value([-0.01, -0.02, 0.005]) == 0.0
    assert one_sided_t_e_value([0.0, 0.0]) == 0.0


def test_e_value_grows_with_consistent_evidence():
    steady = [0.012, 0.009, 0.011, 0.010, 0.008, 0.013]
    assert one_sided_t_e_value(steady[:3]) < one_sided_t_e_value(steady)


# --- the gate -----------------------------------------------------------------


def test_many_correlated_picks_on_one_date_are_one_observation():
    """40 picks from one run all gaining 3% used to clear the 5-lag
    Newey-West t (> 3) on their own: they rode the same market move."""
    db = _memory_db()
    user = _user(db)
    for i in range(40):
        _pick(db, user.id, day=0, excess=0.028 if i % 2 else 0.032, symbol=f"S{i}")
    db.commit()

    result = evaluate_track_record(db, "equity", now=NOW)

    assert result["status"] == "insufficient_data"
    assert result["blocks"] == 1 and result["n"] == 40


def test_a_steady_edge_over_many_blocks_is_proven():
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, STEADY)

    result = evaluate_track_record(db, "equity", now=NOW)

    assert result["status"] == "proven"
    assert result["blocks"] == 12
    assert result["e_value"] >= 1 / EVIDENCE_ALPHA


def test_no_record_is_proven_in_fewer_than_nine_blocks():
    """The e-value is bounded by 2 sqrt(c^2/(n+c^2)) ((n+c^2)/c^2)^(n/2):
    about 93 after 8 blocks, 224 after 9, however strong the evidence."""
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, STEADY[:8])

    result = evaluate_track_record(db, "equity", now=NOW)

    assert result["blocks"] == 8 and result["status"] == "unproven"
    near_certain = [0.01, 0.0101, 0.0099]
    assert one_sided_t_e_value(near_certain * 2 + near_certain[:2]) < 1 / EVIDENCE_ALPHA  # 8 blocks
    assert one_sided_t_e_value(near_certain * 3) >= 1 / EVIDENCE_ALPHA  # 9 blocks


def test_a_mixed_record_is_unproven():
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, [0.02, -0.03, 0.01, -0.015, 0.025, -0.02])

    result = evaluate_track_record(db, "equity", now=NOW)

    assert result["status"] == "unproven"
    assert result["e_value"] < 1 / EVIDENCE_ALPHA


def test_the_cohort_spans_signal_weight_versions():
    """The config id is a hash of the code's default weights; keying on it
    restarted the record at every weight change."""
    db = _memory_db()
    user = _user(db)
    means = STEADY
    for block, mean in enumerate(means):
        config = "cfg-old" if block < 5 else ("cfg-new" if block < 10 else None)
        for j, offset in enumerate((-0.002, 0.0, 0.002)):
            _pick(db, user.id, day=block * 30 + j, excess=mean + offset, config_id=config, symbol=f"S{block}{j}")
    db.commit()

    result = evaluate_track_record(db, "equity", now=NOW)

    assert result["status"] == "proven" and result["blocks"] == 12


def test_instrument_types_are_separate_cohorts():
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, STEADY)
    _monthly(db, user.id, [-0.01] * 12, instrument_type="money_market", hit=False)

    assert evaluate_track_record(db, "equity", now=NOW)["status"] == "proven"
    money_market = evaluate_track_record(db, "money_market", now=NOW)
    assert money_market["status"] == "unproven" and money_market["blocks"] == 12


def test_a_block_still_resolving_ends_the_sequence():
    """Blocks are observed in order: a block with a pick still pending holds
    back that block and every later one."""
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, [0.012, 0.009, 0.011, 0.010, 0.008])
    _pick(db, user.id, day=61, excess=None, status="pending", symbol="LATE")  # block 2
    db.commit()
    now = START + timedelta(days=100)  # block 2's pick resolves at day 91 + 14 days grace

    result = evaluate_track_record(db, "equity", now=now)

    assert result["blocks"] == 2 and result["status"] == "insufficient_data"


def test_a_pick_that_never_resolves_does_not_freeze_the_record():
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, STEADY)
    _pick(db, user.id, day=61, excess=None, status="pending", symbol="DELISTED")
    db.commit()

    result = evaluate_track_record(db, "equity", now=NOW)

    assert result["blocks"] == 12 and result["status"] == "proven"


def test_the_current_block_is_not_judged_until_it_is_over():
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, [0.012, 0.009, 0.011, 0.010])
    now = START + timedelta(days=100)  # inside block 3 (days 90..119)

    assert evaluate_track_record(db, "equity", now=now)["blocks"] == 3


def test_a_losing_majority_is_not_proven_by_a_few_big_winners():
    db = _memory_db()
    user = _user(db)
    _monthly(db, user.id, STEADY, hit=False)

    result = evaluate_track_record(db, "equity", now=NOW)

    assert result["e_value"] >= 1 / EVIDENCE_ALPHA
    assert result["hit_rate"] == 0.0 and result["status"] == "unproven"


def test_no_history_is_insufficient_data():
    db = _memory_db()
    result = evaluate_track_record(db, "equity", now=NOW)
    assert result["status"] == "insufficient_data" and result["blocks"] == 0 and result["n"] == 0
