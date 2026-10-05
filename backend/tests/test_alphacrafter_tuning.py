"""Tests for the quarterly DSR-guarded walk-forward tuning job (todo 20).

Covers: split properties (purge/embargo/chronology), plateau selection over a
sharp-noise argmax, DSR monotonicity in trial count, the full job-handler
integration on memory_db (trial persistence + accepted winner + result_json),
the zero-candidates failure branch, and the manual-trigger endpoint.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base, get_db
from app.main import create_app
from app.foundation.models.entities import (
    AlphacrafterJobRun,
    AlphacrafterTuningTrial,
    AppSetting,
    FactorsLibrary,
    User,
)
from app.lab.alphacrafter.trader import TraderConfig
from app.lab.alphacrafter.tuning import (
    ConfigEvaluation,
    deflated_ic_sharpe,
    evaluate_config,
    newey_west_t_stat,
    run_tuning_job,
    select_config,
    walk_forward_splits,
)
from app.foundation.auth import current_user


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def synthetic_panel():
    """Persistent cross-sectional structure: stable drift ordering per symbol."""
    rng = np.random.default_rng(7)
    n_days, n_symbols = 900, 6
    idx = pd.bdate_range("2022-01-03", periods=n_days)
    drifts = np.linspace(0.0008, 0.004, n_symbols)
    steps = rng.normal(0.0, 0.01, size=(n_days, n_symbols)) + drifts
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(steps, axis=0)),
        index=idx,
        columns=[f"S{i}" for i in range(n_symbols)],
    )
    return {"close": close}


# ---------------------------------------------------------------------------
# 1. Split property tests (AFML ch.7 purge + embargo)
# ---------------------------------------------------------------------------


class TestWalkForwardSplits:
    def test_folds_are_chronological_with_purge_gap(self):
        folds = walk_forward_splits(1200, n_folds=5, purge_days=5, embargo_frac=0.01)
        assert len(folds) == 5
        prev_test_end = -1
        for fold in folds:
            assert fold.test_start > prev_test_end, "test blocks must be disjoint and ordered"
            assert fold.train_end + 5 < fold.test_start, (
                f"purge violated: train_end={fold.train_end}, test_start={fold.test_start}"
            )
            assert len(fold.train_positions) > 0
            prev_test_end = fold.test_end

    def test_embargo_zone_excluded_from_later_training(self):
        n = 1200
        folds = walk_forward_splits(n, n_folds=5, purge_days=5, embargo_frac=0.01)
        embargo_days = max(1, int(round(0.01 * n)))
        for j, fold in enumerate(folds):
            forbidden: set[int] = set()
            for earlier in folds[:j]:
                # Embargo starts one row past the last test row.
                forbidden.update(range(earlier.test_end + 1, earlier.test_end + 1 + embargo_days))
            overlap = set(fold.train_positions.tolist()) & forbidden
            assert not overlap, f"fold {j} trains on embargoed rows: {sorted(overlap)[:5]}"

    def test_test_blocks_cover_sample(self):
        n = 1200
        folds = walk_forward_splits(n, n_folds=5, purge_days=5, embargo_frac=0.01)
        covered = [p for f in folds for p in f.test_positions.tolist()]
        first_test = folds[0].test_start
        assert covered == list(range(first_test, n)), (
            "test blocks must tile the post-warmup sample exactly"
        )

    def test_min_five_folds_enforced(self):
        with pytest.raises(ValueError):
            walk_forward_splits(1000, n_folds=4)


# ---------------------------------------------------------------------------
# 2. Newey-West t-stat + DSR monotonicity
# ---------------------------------------------------------------------------


class TestSignificanceStats:
    def test_newey_west_t_stat_on_iid_series(self):
        rng = np.random.default_rng(11)
        x = rng.normal(0.5, 0.1, 400).tolist()
        t_stat = newey_west_t_stat(x, lags=5)
        # mean 0.5, se ~ 0.1/sqrt(400) = 0.005 → t ~ 100
        assert t_stat > 20

    def test_newey_west_degenerate_returns_zero(self):
        assert newey_west_t_stat([0.05] * 50, lags=5) == 0.0
        assert newey_west_t_stat([], lags=5) == 0.0

    def test_deflated_ic_sharpe_monotonic_in_trials(self):
        rng = np.random.default_rng(3)
        x = rng.normal(0.05, 0.15, 300).tolist()
        d_low = deflated_ic_sharpe(x, n_trials=5)
        d_high = deflated_ic_sharpe(x, n_trials=5000)
        assert 0.0 <= d_high <= d_low <= 1.0, (
            f"more trials must raise the threshold: d(5)={d_low}, d(5000)={d_high}"
        )


# ---------------------------------------------------------------------------
# 3. Plateau selection beats sharp-noise argmax
# ---------------------------------------------------------------------------

_GRID = [TraderConfig(rebalance_freq=f, position_size=s, commission=0.001)
         for f in ("W", "M") for s in (0.05, 0.1, 0.2)]


def _ev(freq, size, comm, oos_mean_ic, oos_icir):
    """Evaluation with a deterministic OOS IC series whose NW t-stat tracks the mean."""
    series = (oos_mean_ic + np.tile([0.02, -0.02], 25)).tolist()
    return ConfigEvaluation(
        config=TraderConfig(rebalance_freq=freq, position_size=size, commission=comm),
        is_mean_ic=oos_mean_ic * 1.2,
        oos_mean_ic=oos_mean_ic,
        oos_icir=oos_icir,
        wfe=1.0,
        oos_ic_series=series,
        per_fold_oos_mean_ic=[oos_mean_ic] * 5,
    )


class TestPlateauSelection:
    def test_plateau_center_selected_over_sharp_noise_argmax(self):
        evals = [
            _ev("W", 0.05, 0.001, 0.05, 1.2),
            _ev("W", 0.1, 0.001, 0.05, 1.5),
            _ev("W", 0.2, 0.001, 0.05, 1.1),
            _ev("M", 0.05, 0.001, 0.01, 0.2),  # fails min_ic
            _ev("M", 0.1, 0.001, 0.06, 9.0),   # sharp-noise argmax, neighbours fail
            _ev("M", 0.2, 0.001, 0.01, 0.3),   # fails min_ic
        ]
        result = select_config(evals, _GRID, min_ic=0.03, min_icir=1.0, horizon=5)
        argmax = max(evals, key=lambda ev: ev.oos_icir)
        assert result.selected is not None
        assert result.selected.config == TraderConfig("W", 0.1, 0.001), (
            "plateau centre must beat the isolated sharp argmax"
        )
        assert result.selected.oos_icir < argmax.oos_icir
        assert result.basis in {"strict_plateau", "largest_plateau_center"}
        assert result.consensus_config["rebalance_freq"] in {"W", "M"}

    def test_largest_plateau_center_when_no_strict_plateau(self):
        evals = [
            _ev("W", 0.05, 0.001, 0.05, 1.2),
            _ev("W", 0.1, 0.001, 0.05, 1.5),
            _ev("W", 0.2, 0.001, 0.05, 1.1),
            _ev("M", 0.05, 0.001, 0.01, 0.2),
            _ev("M", 0.1, 0.001, 0.01, 0.2),
            _ev("M", 0.2, 0.001, 0.01, 0.3),
        ]
        result = select_config(evals, _GRID, min_ic=0.03, min_icir=1.0, horizon=5)
        assert result.selected.config == TraderConfig("W", 0.1, 0.001)
        assert result.basis == "largest_plateau_center"

    def test_zero_passing_yields_none(self):
        evals = [_ev("W", 0.1, 0.001, 0.001, 0.1)]
        result = select_config(evals, _GRID, min_ic=0.99, min_icir=1.0, horizon=5)
        assert result.selected is None
        assert result.basis == "none"
        assert "No acceptable configuration" in result.reason


# ---------------------------------------------------------------------------
# 4. Per-config evaluation on a real synthetic panel
# ---------------------------------------------------------------------------


class TestEvaluateConfig:
    def test_stitched_oos_series_and_positive_ic(self, synthetic_panel):
        close = synthetic_panel["close"]
        composite = close.rank(axis=1)
        folds = walk_forward_splits(len(close) - 252, n_folds=5, purge_days=5)
        config = TraderConfig(rebalance_freq="W", position_size=0.1, commission=0.001)
        ev = evaluate_config(close.iloc[:-252], composite.iloc[:-252], config, folds, horizon=5)
        assert len(ev.oos_ic_series) > 50
        assert ev.oos_mean_ic > 0.15, f"persistent structure should give positive IC: {ev.oos_mean_ic}"
        assert ev.wfe > 0.5
        assert len(ev.per_fold_oos_mean_ic) == 5


# ---------------------------------------------------------------------------
# 5. Job handler integration on memory_db
# ---------------------------------------------------------------------------


def _seed_tuning_db(db, *, min_ic="0.0"):
    db.add(AppSetting(key="alphacrafter_min_ic", value_json=min_ic))
    db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
    db.add(AppSetting(key="alphacrafter_rebalance_freqs", value_json='"W"'))
    db.add(AppSetting(key="alphacrafter_position_sizes", value_json='"0.1,0.2"'))
    db.add(AppSetting(key="alphacrafter_commissions", value_json='"0.001"'))
    factor = FactorsLibrary(
        name="rank_close",
        formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": "rank_close"}),
        source="test",
        ic_summary_json=json.dumps({"ic": 0.5}),
    )
    db.add(factor)
    db.commit()


class TestTuningJobIntegration:
    def test_persists_trials_accepted_winner_and_result_json(self, synthetic_panel):
        db = _memory_db()
        _seed_tuning_db(db)

        with patch("app.lab.alphacrafter.tuning.build_panel", return_value=synthetic_panel):
            result = asyncio.run(run_tuning_job(db))

        trials = db.execute(select(AlphacrafterTuningTrial)).scalars().all()
        assert len(trials) == 2, "every grid combo must be logged as a trial"
        accepted = [t for t in trials if t.accepted]
        assert len(accepted) == 1, "exactly one configuration may be accepted"

        job_rows = db.execute(select(AlphacrafterJobRun)).scalars().all()
        assert len(job_rows) == 1
        assert job_rows[0].status == "completed"
        parsed = json.loads(job_rows[0].result_json)
        assert parsed["job_type"] == "alphacrafter_tuning"
        assert parsed["n_trials"] == 2
        assert parsed["accepted_config"]["rebalance_freq"] == "W"
        assert parsed["holdout"]["n_obs"] > 0, "terminal holdout evaluated once post-selection"
        assert parsed["selection_basis"] in {"strict_plateau", "largest_plateau_center"}
        assert "message" in result

    def test_zero_passing_candidates_leaves_settings_untouched(self, synthetic_panel):
        db = _memory_db()
        _seed_tuning_db(db, min_ic="0.99")
        settings_before = db.query(AppSetting).count()

        with patch("app.lab.alphacrafter.tuning.build_panel", return_value=synthetic_panel):
            result = asyncio.run(run_tuning_job(db))

        trials = db.execute(select(AlphacrafterTuningTrial)).scalars().all()
        assert len(trials) == 2
        assert all(not t.accepted for t in trials)
        assert "No acceptable configuration" in result["message"]
        assert db.query(AppSetting).count() == settings_before, "settings must stay untouched"


# ---------------------------------------------------------------------------
# 6. Quarterly cron registration + manual-trigger endpoint
# ---------------------------------------------------------------------------


class TestTuningRegistrationAndEndpoint:
    def test_quarterly_cron_registration(self):
        from apscheduler.schedulers.background import BackgroundScheduler

        from app.lab.alphacrafter.jobs import register_alphacrafter_tuning_job

        scheduler = BackgroundScheduler()
        scheduler.start()
        try:
            job_id = register_alphacrafter_tuning_job(scheduler)
            job = scheduler.get_job(job_id)
            assert job is not None
            assert job.id == "alphacrafter_tuning"
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert fields["month"] == "1,4,7,10", f"quarterly schedule expected: {fields}"
        finally:
            scheduler.shutdown(wait=False)

    def test_manual_trigger_endpoint_returns_job_id(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = maker()
        user = User(username="jan", password_hash="hash", role="admin")
        db.add(user)
        db.commit()
        db.refresh(user)

        app = create_app()

        def test_db():
            session = maker()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[current_user] = lambda: maker().get(User, user.id)

        client = TestClient(app)
        with patch(
            "app.lab.alphacrafter.tuning.run_tuning_job_sync",
            return_value={"job_type": "alphacrafter_tuning"},
        ):
            response = client.post("/api/alphacrafter/tuning/run")

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["job_id"], body
        assert body["status"] == "running"

        check = maker()
        row = check.get(AlphacrafterJobRun, body["job_id"])
        assert row is not None
        assert row.status == "running"
