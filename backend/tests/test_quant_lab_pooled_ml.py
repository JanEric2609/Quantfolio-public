"""The pooled cross-sectional ML model behind Discover's ml_signal (ADR 0015 #24)."""
from __future__ import annotations

import json
import uuid

import numpy as np
import pandas as pd
import pytest
from conftest import _memory_db

from app.foundation.models.entities import QuantMlModel, TrialLedgerEntry, User
from app.lab.quant_lab import pooled_ml as pooled


def _panel(*, n_symbols: int = 60, n_days: int = 700, drift_sd: float = 0.0, seed: int = 3) -> pd.DataFrame:
    """Random-walk closes; with drift_sd > 0 each symbol keeps its own daily
    drift, so its past return predicts its future one (a planted signal)."""
    rng = np.random.default_rng(seed)
    drift = rng.normal(0.0, drift_sd, n_symbols) if drift_sd else np.zeros(n_symbols)
    rets = rng.normal(0.0, 0.01, (n_days, n_symbols)) + drift
    idx = pd.bdate_range("2023-01-02", periods=n_days)
    return pd.DataFrame(100.0 * np.cumprod(1.0 + rets, axis=0), index=idx,
                        columns=[f"S{i:02d}" for i in range(n_symbols)])


def _user(db) -> User:
    user = User(id=str(uuid.uuid4()), username=f"u-{uuid.uuid4().hex[:6]}", password_hash="x")
    db.add(user)
    db.commit()
    return user


def test_features_and_label_never_look_ahead():
    close = _panel(n_symbols=3, n_days=400)
    label = pooled.forward_label(close)
    feats = pooled.raw_features(close)
    sym, d = "S00", close.index[300]
    s = close[sym]
    expected = (s.shift(-20) / s - 1) / s.pct_change().rolling(60, min_periods=60).std()
    assert label.at[d, sym] == pytest.approx(expected.loc[d])
    assert label[sym].iloc[-pooled.HORIZON:].isna().all()
    assert feats["ret_5d"].at[d, sym] == pytest.approx(s.loc[d] / s.shift(5).loc[d] - 1)
    # Truncating the future leaves every feature on d unchanged.
    cut = pooled.raw_features(close.loc[:d])
    for name in pooled.FEATURES:
        assert cut[name].at[d, sym] == pytest.approx(feats[name].at[d, sym], nan_ok=True)


def test_features_follow_each_symbols_own_calendar():
    """A US holiday is NaN for US names in a mixed EU/US panel; it must not
    blank out a full-window rolling feature for the next 21 sessions."""
    close = _panel(n_symbols=2, n_days=400)
    close.iloc[350, 1] = np.nan  # S01 closed that day
    feats = pooled.raw_features(close)
    assert feats["vol_21d"]["S01"].iloc[351:372].notna().all()
    assert np.isnan(feats["vol_21d"].iloc[350, 1])


def test_a_planted_signal_passes_the_gate():
    study, frame = pooled.run_study(_panel(drift_sd=0.0015))
    assert study.served.ic > 0.1
    assert study.served.passed
    assert frame["label"].between(-0.5, 0.5).all()
    assert len(study.folds) == pooled.N_FOLDS


def test_noise_is_rejected():
    study, _ = pooled.run_study(_panel(drift_sd=0.0))
    assert not study.served.passed
    assert abs(study.served.ic) < pooled.MIN_IC * 2


def test_training_persists_the_study_and_records_each_model_once(tmp_path):
    db = _memory_db()
    user = _user(db)

    row = pooled.train_pooled_model(db, user.id, _panel(drift_sd=0.0015), model_dir=str(tmp_path))
    assert row.status == "completed"
    assert row.ticker is None and row.name == pooled.POOLED_MODEL_NAME
    assert row.artefact_path and row.artefact_path.startswith(str(tmp_path))
    metrics = json.loads(row.metrics_json)
    assert metrics["served_model"] == "gbm" and metrics["models"]["gbm"]["passed"]

    pooled.train_pooled_model(db, user.id, _panel(drift_sd=0.0015, seed=4), model_dir=str(tmp_path))
    trials = db.query(TrialLedgerEntry).filter(TrialLedgerEntry.context == pooled.TRIAL_CONTEXT).all()
    assert sorted(t.trial_key.split(":")[0] for t in trials) == ["gbm", "ridge"]  # refits are not new trials
    assert pooled.latest_pooled_row(db).id != row.id


def test_a_rejected_model_gets_no_artefact(tmp_path):
    db = _memory_db()
    user = _user(db)
    row = pooled.train_pooled_model(db, user.id, _panel(drift_sd=0.0), model_dir=str(tmp_path))
    assert row.status == "rejected_below_gate"
    assert row.artefact_path is None
    assert "rank IC" in row.error_message and "momentum 12-1" in row.error_message
    assert not list(tmp_path.iterdir())


def test_serving_ranks_within_the_given_cross_section():
    close = _panel(drift_sd=0.0015)
    study, frame = pooled.run_study(close)
    model = pooled.fit_served(frame)
    raw = pooled.latest_raw_features(close)
    pct = pooled.score_cross_section(model, raw)
    assert set(pct.index) == set(close.columns)
    assert pct.between(0.0, 1.0).all()
    # The model learned drift persistence: the best past performer ranks high.
    best = raw["ret_126d"].astype(float).idxmax()
    assert pct[best] > 0.7


def test_spec_hash_is_stable():
    assert pooled.spec_hash() == pooled.spec_hash()
    assert len(pooled.spec_hash()) == 16


def test_served_rows_are_not_confused_with_per_ticker_models():
    db = _memory_db()
    user = _user(db)
    db.add(QuantMlModel(user_id=user.id, name="discover-ml-AAPL", kind="lgbm", ticker="AAPL", status="completed",
                        feature_schema_version=pooled.POOLED_SCHEMA_VERSION))
    db.commit()
    assert pooled.latest_pooled_row(db) is None


def test_weekly_job_trains_once_then_respects_the_cooldown(monkeypatch, tmp_path):
    """discover_ml_training builds the miner universe's close panel from
    stored bars and trains the pooled model once for everybody."""
    from datetime import UTC, datetime

    from app.decision.discover import jobs as discover_jobs
    from app.foundation.models.entities import DiscoverRun

    db = _memory_db()
    user = _user(db)
    db.add(DiscoverRun(user_id=user.id, status="completed", completed_at=datetime.now(UTC)))
    db.commit()

    captured = {}
    monkeypatch.setattr(discover_jobs, "register_cron_job",
                        lambda name, fn, **kw: captured.setdefault(name, fn) and name)
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr("app.lab.alphacrafter.panel.build_panel",
                        lambda db, universe, start, end, include_fundamentals=False: {"close": _panel(drift_sd=0.0)})
    monkeypatch.setattr("app.lab.quant_ml.registry._DEFAULT_BASE", str(tmp_path))

    discover_jobs.register_discover_ml_training_job()
    captured["discover_ml_training"]()
    captured["discover_ml_training"]()

    rows = db.query(QuantMlModel).filter(QuantMlModel.name == pooled.POOLED_MODEL_NAME).all()
    assert len(rows) == 1
    assert rows[0].user_id == user.id
    assert rows[0].status == "rejected_below_gate"
