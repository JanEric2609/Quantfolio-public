"""Tests for the pooled cross-sectional model (report Phase 3, step 2)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering import _pit_duckdb
from app.foundation.models.entities import TrialLedgerEntry
from app.lab.pooled_model import month_series, run_model_study
from app.lab.pooled_model import fit as fit_mod
from app.lab.pooled_model import study as study_mod
from app.lab.pooled_model import scores_path
from app.lab.pooled_model.panel import centred_ranks, iter_chunks, model_panel_path, panel_months
from app.lab.pooled_model.spec import PURGE_MONTHS
from app.lab.quant_lab.cv import purged_embargo_splits


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture(autouse=True)
def _duckdb(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_MEMORY_LIMIT", "256MB")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_THREADS", "1")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_TMPDIR", str(tmp_path / "duckdb"))
    # One LightGBM thread: the suite runs six xdist workers on shared cores.
    monkeypatch.setattr(fit_mod, "GBM_THREADS", 1)
    _pit_duckdb.reset_connection()
    yield
    _pit_duckdb.reset_connection()


def _panel(root: Path, months: int = 130, stocks: int = 30, name: str = "x.0000", seed: int = 0) -> Path:
    """Two countries; next-month return driven by gp_at, value and momentum pure noise."""
    rng = np.random.default_rng(seed)
    rows = []
    for m, eom in enumerate(pd.date_range("2000-01-31", periods=months, freq="ME")):
        for country in ("USA", "DEU"):
            gp = rng.normal(size=stocks)
            for i in range(stocks):
                rows.append({
                    "gvkey": f"{country}{i}", "permno": None, "eom": eom, "excntry": country, "size_grp": "large",
                    "me": 100.0 + i, "be_me": rng.normal(), "mom_12_1": rng.normal(), "gp_at": gp[i],
                    "at_gr1": rng.normal(), "ret_exc_lead1m": 0.02 * gp[i] + 0.005 * rng.normal(),
                    "source": "wrds_factor_characteristics", "ingested_at": pd.Timestamp("2026-09-01"),
                })
    out = root / "wrds_factor_characteristics_pit"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(out / f"{name}.parquet", index=False)
    return root


def test_centred_ranks_are_symmetric_and_missing_is_middle():
    frame = pd.DataFrame({
        "eom": ["a"] * 4, "excntry": ["X"] * 4, "v": [1.0, 2.0, 3.0, np.nan],
    })
    ranks = centred_ranks(frame, ["v"])[:, 0]
    assert ranks[:3] == pytest.approx([-1 / 3, 0.0, 1 / 3])
    assert ranks[3] == 0.0


def test_month_series_terciles_match_factor_premia():
    # Same case as factor_premia's tercile test: 15 equal-cap stocks, rank i earns i %.
    frame = pd.DataFrame({
        "eom": pd.Timestamp("2020-01-31"), "excntry": "DEU", "me": 100.0,
        "r": [i / 100 for i in range(15)], "y": [(i + 0.5) / 15 - 0.5 for i in range(15)],
        "score": [float(i) for i in range(15)],
    })
    row = month_series(frame, "score").iloc[0]
    assert str(row["month"]) == "2020-02"
    assert row["long_short"] == pytest.approx(0.10)
    assert row["long_only"] == pytest.approx(0.05)
    assert row["ic"] == pytest.approx(1.0)
    assert row["n_stocks"] == 15


def test_a_score_that_cannot_rank_a_month_holds_the_market():
    frame = pd.DataFrame({
        "eom": pd.Timestamp("2020-01-31"), "excntry": "DEU", "me": 100.0,
        "r": [i / 100 for i in range(15)], "y": [(i + 0.5) / 15 - 0.5 for i in range(15)], "score": 1.0,
    })
    row = month_series(frame, "score").iloc[0]
    assert row["long_short"] == 0.0 and row["long_only"] == 0.0
    assert np.isnan(row["ic"])


def test_models_find_a_planted_signal_the_baseline_does_not_have(tmp_path):
    study = run_model_study(_memory_db(), panel_dir=_panel(tmp_path), persist=False, n_folds=5)

    assert study is not None
    cards = {c.model: c for c in study.cards}
    assert cards["ridge"].ic_mean > 0.8
    assert cards["ridge"].long_short_annual > 0.2
    assert cards["ridge"].vs_baseline_annual > 0.2
    assert cards["gbm"].ic_mean > 0.3
    assert abs(cards["value_momentum"].ic_mean) < 0.1
    # Out of sample starts after the first training block, never at month one.
    assert cards["ridge"].months < 130
    assert len(study.folds) == 5


def test_every_fold_trains_only_on_earlier_purged_months(tmp_path, monkeypatch):
    seen: list[np.ndarray] = []
    real = study_mod.fit_ridge
    monkeypatch.setattr(study_mod, "fit_ridge", lambda moments, train: seen.append(train) or real(moments, train))

    run_model_study(_memory_db(), panel_dir=_panel(tmp_path), persist=False, n_folds=5)

    folds = purged_embargo_splits(130, n_folds=5, purge_days=PURGE_MONTHS, embargo_frac=1 / 130)
    assert len(seen) == len(folds)
    for train, fold in zip(seen, folds, strict=True):
        assert train.max() < fold.test_positions.min() - PURGE_MONTHS + 1
        assert not set(train) & set(fold.test_positions)


def test_panel_file_is_reused_until_the_extract_changes(tmp_path):
    panel = _panel(tmp_path, months=12)
    first = model_panel_path(panel, "world")
    assert first is not None and model_panel_path(panel, "world") == first

    scores = scores_path(panel, "world")
    scores.write_bytes(b"kept")
    _panel(tmp_path, months=12, name="x.0001", seed=1)
    second = model_panel_path(panel, "world")
    assert second is not None and second != first
    assert second.exists() and not first.exists()
    assert scores.exists()


def test_panel_rows_are_in_a_total_order(tmp_path):
    # The boosted trees see a fixed-seed sample picked by row position, so a
    # rebuild must put the rows in the same order, not just the same months.
    path = model_panel_path(_panel(tmp_path, months=12), "world")
    assert path is not None
    rows = pd.read_parquet(path, columns=["eom", "excntry", "gvkey"])
    pd.testing.assert_frame_equal(rows, rows.sort_values(["eom", "excntry", "gvkey"]).reset_index(drop=True))
    chunks = list(iter_chunks(path, panel_months(path), months_per_chunk=5))
    assert np.concatenate([c.gvkey for c in chunks]).tolist() == rows["gvkey"].tolist()


def test_persisted_run_records_each_model_once_and_writes_results(tmp_path):
    db = _memory_db()
    panel = _panel(tmp_path)
    study = run_model_study(db, panel_dir=panel, n_folds=5)
    run_model_study(db, panel_dir=panel, n_folds=5)

    keys = sorted(e.trial_key for e in db.query(TrialLedgerEntry).filter_by(context="pooled_model"))
    assert keys == ["world:gbm", "world:ridge"]
    assert study is not None and study.artifact is not None
    saved = json.loads(study.artifact.read_text())
    assert {c["model"] for c in saved["cards"]} == {"ridge", "gbm", "value_momentum"}
    assert len(saved["series"]) == 3 * study.cards[0].months

    scores = pd.read_parquet(scores_path(panel, "world"))
    assert list(scores.columns) == ["eom", "gvkey", "excntry", "size_grp", "me", "r", "ridge", "gbm", "value_momentum"]
    # Only out-of-sample months, every stock of each: two countries of 30.
    assert len(scores) == study.cards[0].months * 60
    assert scores["gvkey"].str.match(r"^(USA|DEU)\d+$").all()


def test_a_dry_run_writes_no_scores(tmp_path):
    panel = _panel(tmp_path)
    run_model_study(_memory_db(), panel_dir=panel, persist=False, n_folds=5)
    assert not scores_path(panel, "world").exists()


def test_no_panel_means_no_study(tmp_path):
    assert run_model_study(_memory_db(), panel_dir=tmp_path, persist=False) is None
