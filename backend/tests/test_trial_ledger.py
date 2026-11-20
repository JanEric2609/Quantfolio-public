"""Tests for the global trial ledger + n_trials resolver + CSCV/PBO
(ADR 0015, Phase 1 — Honest Measurement Rebuild)."""

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import TrialLedgerEntry
from app.foundation.quant_metrics import (
    DEFAULT_N_TRIALS_FLOOR,
    probability_of_backtest_overfitting,
    record_trial,
    resolve_n_trials,
)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ---------------------------------------------------------------------------
# record_trial / resolve_n_trials
# ---------------------------------------------------------------------------


def test_resolve_n_trials_is_at_least_one_on_an_empty_ledger():
    db = _memory_db()
    assert DEFAULT_N_TRIALS_FLOOR == 1
    assert resolve_n_trials(db) == 1


def test_resolve_n_trials_is_the_real_ledger_count():
    db = _memory_db()
    for i in range(18):
        record_trial(db, context="alphacrafter_tuning", trial_key=f"job-1:{i}")
    db.commit()
    assert resolve_n_trials(db) == 18


def test_resolve_n_trials_accepts_a_custom_floor():
    db = _memory_db()
    record_trial(db, context="advisor_challenger", trial_key="a")
    db.commit()
    assert resolve_n_trials(db, floor=50) == 50


def test_effective_n_trials_counts_search_families():
    """Configurations of one tuning run are one family, not independent tests."""
    from app.foundation.quant_metrics import effective_n_trials

    db = _memory_db()
    for run in ("r1", "r2"):
        for i in range(10):
            record_trial(db, "alphacrafter_tuning", f"{run}:{i}", {"job_run_id": run})
    record_trial(db, "advisor_challenger", "a")
    db.commit()
    assert resolve_n_trials(db) == 21
    assert effective_n_trials(db) == 3


def test_dsr_hurdle_rises_with_n():
    """Expected max Sharpe of N null trials over 5y daily (False Strategy Theorem)."""
    from app.foundation.quant_metrics import dsr_hurdle_curve

    curve = {row["n_trials"]: row["sharpe_hurdle"] for row in dsr_hurdle_curve([1, 18, 500], 1260)}
    assert curve[1] == 0.0
    assert abs(curve[18] - 0.829) < 0.002
    assert abs(curve[500] - 1.365) < 0.002


def test_record_trial_is_idempotent_on_context_and_key():
    """A retried job must not double-count the same hypothesis test."""
    db = _memory_db()
    first = record_trial(db, context="alphacrafter_tuning", trial_key="job-1:cfg-a")
    db.commit()
    second = record_trial(db, context="alphacrafter_tuning", trial_key="job-1:cfg-a")
    db.commit()
    assert first.id == second.id
    assert db.query(TrialLedgerEntry).count() == 1


def test_record_trial_distinguishes_context():
    """The same trial_key in a different context is a distinct trial."""
    db = _memory_db()
    record_trial(db, context="alphacrafter_tuning", trial_key="k")
    record_trial(db, context="advisor_challenger", trial_key="k")
    db.commit()
    assert db.query(TrialLedgerEntry).count() == 2


def test_record_trial_persists_metadata():
    db = _memory_db()
    entry = record_trial(
        db, context="discover_candidate", trial_key="cfg-1", metadata={"config_id": "cfg-1"},
    )
    db.commit()
    assert entry.metadata_json == {"config_id": "cfg-1"}


def test_resolve_n_trials_aggregates_across_contexts():
    """A gate in ANY context sees the search breadth spent in every context —
    that's the point (un-gameable global count)."""
    db = _memory_db()
    for i in range(300):
        record_trial(db, context="alphacrafter_tuning", trial_key=f"a{i}")
    for i in range(250):
        record_trial(db, context="discover_candidate", trial_key=f"b{i}")
    db.commit()
    assert resolve_n_trials(db) == 550


# ---------------------------------------------------------------------------
# probability_of_backtest_overfitting (CSCV)
# ---------------------------------------------------------------------------


def test_pbo_degenerate_inputs_fail_closed():
    """Too few candidates, an odd/too-small partition count, or too little
    history must not silently report an optimistic 0.0."""
    rng = np.random.default_rng(0)
    single_column = rng.normal(size=(400, 1))
    assert probability_of_backtest_overfitting(single_column) == 1.0

    two_cols_short = rng.normal(size=(10, 2))
    assert probability_of_backtest_overfitting(two_cols_short) == 1.0

    ample = rng.normal(size=(400, 4))
    assert probability_of_backtest_overfitting(ample, n_partitions=3) == 1.0  # odd
    assert probability_of_backtest_overfitting(ample, n_partitions=1) == 1.0  # < 2


def test_pbo_returns_a_probability_in_unit_interval():
    rng = np.random.default_rng(1)
    m = rng.normal(scale=0.01, size=(500, 6))
    pbo = probability_of_backtest_overfitting(m, n_partitions=10)
    assert 0.0 <= pbo <= 1.0


def test_pbo_is_low_when_one_strategy_genuinely_dominates():
    """A single column with a real, consistent edge over pure noise columns
    should rank well OOS whenever it ranks well IS — low overfitting."""
    rng = np.random.default_rng(2)
    t = 1000
    noise_cols = rng.normal(scale=0.01, size=(t, 5))
    skilled_col = rng.normal(loc=0.01, scale=0.01, size=(t, 1))
    m = np.hstack([skilled_col, noise_cols])
    pbo = probability_of_backtest_overfitting(m, n_partitions=10)
    assert pbo < 0.5


def test_pbo_is_high_when_selection_is_pure_noise():
    """When every column is pure zero-mean noise, whichever one looks best
    in-sample is a coin flip out-of-sample — PBO should sit near 0.5, not
    near 0 (which would mean the noise columns somehow reliably persist)."""
    rng = np.random.default_rng(3)
    m = rng.normal(scale=0.01, size=(1200, 8))
    pbo = probability_of_backtest_overfitting(m, n_partitions=8)
    assert pbo > 0.3


def test_pbo_rejects_non_2d_input():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(np.array([1.0, 2.0, 3.0]))
