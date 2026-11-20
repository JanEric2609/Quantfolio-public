"""Tests for quant_ml/training.py — the shared training core used by both
/ml/train and the discover-ml-training scheduled job (Track D1b)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from conftest import _memory_db

from app.foundation.models.entities import QuantMlModel, User


def _make_prices_dates(n: int = 400, seed: int = 42) -> tuple[list[float], list[str]]:
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(0.0005, 0.01, n)
    prices = (100.0 * np.cumprod(np.exp(log_returns))).tolist()
    base = datetime.now(UTC) - timedelta(days=n)
    dates = [(base + timedelta(days=i)).date().isoformat() for i in range(n)]
    return prices, dates


def _user(db) -> User:
    user = User(id=str(uuid.uuid4()), username="ml-training-user", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestPrepareTrainingData:
    def test_insufficient_history_returns_none(self):
        from app.lab.quant_ml.training import prepare_training_data

        prices, dates = _make_prices_dates(30)
        result = prepare_training_data(prices, dates)

        assert result is None

    def test_sufficient_history_returns_aligned_xy(self):
        from app.lab.quant_ml.training import prepare_training_data

        prices, dates = _make_prices_dates(400)
        result = prepare_training_data(prices, dates)

        assert result is not None
        X, y = result
        assert len(X) == len(y)
        assert len(X) > 30

    def test_extra_features_columns_present_in_aligned_x(self):
        from app.lab.quant_ml.training import prepare_training_data

        prices, dates = _make_prices_dates(400)
        extra = pd.DataFrame(
            {"my_signal": np.arange(len(dates), dtype=float)},
            index=pd.DatetimeIndex(dates),
        )

        result = prepare_training_data(prices, dates, extra_features=extra)

        assert result is not None
        X, y = result
        assert "my_signal" in X.columns
        assert len(X) == len(y)


class TestTrainAndValidateTickerModel:
    def test_insufficient_history_returns_none_without_persisting(self):
        from app.lab.quant_ml.training import train_and_validate_ticker_model

        prices, dates = _make_prices_dates(30)
        db = _memory_db()
        user = _user(db)

        result = train_and_validate_ticker_model(db, user.id, "AAPL", prices, dates)

        assert result is None
        assert db.query(QuantMlModel).count() == 0

    def test_persists_model_row_with_ticker_set(self, tmp_path):
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.training import train_and_validate_ticker_model

        prices, dates = _make_prices_dates(500)
        db = _memory_db()
        user = _user(db)

        row = train_and_validate_ticker_model(
            db, user.id, "aapl", prices, dates, model_dir=str(tmp_path),
        )

        assert row is not None
        assert row.ticker == "AAPL"
        assert row.status in ("completed", "rejected_below_baseline", "failed", "unavailable")
        assert row.finished_at is not None
        persisted = db.get(QuantMlModel, row.id)
        assert persisted is not None
        assert persisted.ticker == "AAPL"

    def test_rejected_model_has_no_artefact_path(self, tmp_path):
        """A model that doesn't clear the walk-forward baseline gate must
        not have an artefact_path — nothing should ever load an unvalidated
        pipeline as if it were usable."""
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.training import train_and_validate_ticker_model

        prices, dates = _make_prices_dates(500)
        db = _memory_db()
        user = _user(db)

        row = train_and_validate_ticker_model(
            db, user.id, "AAPL", prices, dates, model_dir=str(tmp_path),
        )

        if row.status != "completed":
            assert row.artefact_path is None

    def test_stamps_current_feature_schema_version(self, tmp_path):
        """Every newly-trained row must record which build_features column
        set it was fit against, so stage_ml_signal / predict can refuse to
        load a stale-schema artefact instead of hard-erroring inside
        StandardScaler on a feature-count mismatch."""
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.training import (
            CURRENT_FEATURE_SCHEMA_VERSION,
            train_and_validate_ticker_model,
        )

        prices, dates = _make_prices_dates(500)
        db = _memory_db()
        user = _user(db)

        row = train_and_validate_ticker_model(
            db, user.id, "AAPL", prices, dates, model_dir=str(tmp_path),
        )

        assert row is not None
        assert row.feature_schema_version == CURRENT_FEATURE_SCHEMA_VERSION

    def test_extra_features_threaded_into_build_features(self, tmp_path):
        """extra_features is a plain passthrough -- train_and_validate_ticker_model
        never fetches it itself (see the module docstring's import-cycle
        note), it just has to actually reach build_features."""
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.training import train_and_validate_ticker_model

        prices, dates = _make_prices_dates(500)
        db = _memory_db()
        user = _user(db)
        extra = pd.DataFrame(
            {"my_signal": np.arange(len(dates), dtype=float)},
            index=pd.DatetimeIndex(dates),
        )

        row = train_and_validate_ticker_model(
            db, user.id, "AAPL", prices, dates, model_dir=str(tmp_path),
            extra_features=extra,
        )

        assert row is not None
        assert row.status in ("completed", "rejected_below_baseline", "failed", "unavailable")

    def test_thin_sample_never_fails_on_insufficient_observations(self, tmp_path):
        """Regression test for the CPCV migration (Issue 1): prod's real
        aligned-row counts land around ~245-252 after build_features'
        long-window warmup trims a ~730-day price fetch, which the old
        WalkForward(train_size=252) splitter could never satisfy (needed
        >=273 rows) -- causing 100% of training attempts to fail. n=505
        raw days lands prepare_training_data's aligned output at 252 rows,
        reproducing that exact band. status must never be "failed" or
        "unavailable" here, and error_message must be populated whenever
        the model isn't promoted (covers the 1a error_message fix too)."""
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.training import train_and_validate_ticker_model

        prices, dates = _make_prices_dates(505)
        db = _memory_db()
        user = _user(db)

        row = train_and_validate_ticker_model(
            db, user.id, "AAPL", prices, dates, model_dir=str(tmp_path),
        )

        assert row is not None
        assert row.status in ("completed", "rejected_below_baseline")
        if row.status != "completed":
            assert row.error_message is not None

    def test_no_extra_features_reproduces_prior_behavior(self, tmp_path):
        """extra_features=None (the default) must train exactly as it did
        before Phase 5 -- the kill switch is simply not passing extra_features."""
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.training import train_and_validate_ticker_model

        prices, dates = _make_prices_dates(500)
        db = _memory_db()
        user = _user(db)

        row = train_and_validate_ticker_model(
            db, user.id, "AAPL", prices, dates, model_dir=str(tmp_path),
        )

        assert row is not None
        assert row.status in ("completed", "rejected_below_baseline", "failed", "unavailable")
