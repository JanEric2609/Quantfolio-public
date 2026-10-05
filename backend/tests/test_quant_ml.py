"""Tests for quant_ml: labels, features, and pipelines."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prices(n: int = 300, seed: int = 42, start: float = 100.0) -> list[float]:
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(0.0005, 0.01, n)
    prices = start * np.cumprod(np.exp(log_returns))
    return prices.tolist()


def _make_synthetic_xy(n: int = 200, n_features: int = 6, seed: int = 42):
    """Return (X: DataFrame, y: Series) for classifier training."""
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(
        rng.standard_normal((n, n_features)),
        columns=[f"f{i}" for i in range(n_features)],
    )
    y = pd.Series(rng.choice([-1, 0, 1], size=n, p=[0.33, 0.34, 0.33]))
    return X, y


def _make_signal_xy(n: int = 400, n_features: int = 4, seed: int = 42):
    """Return (X, y) with real linear signal, unlike ``_make_synthetic_xy``'s
    pure noise — needed for walk_forward_cv's majority-class baseline gate,
    which rejects a fitted pipeline that can't beat chance."""
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(
        rng.standard_normal((n, n_features)),
        columns=[f"f{i}" for i in range(n_features)],
    )
    score = 2.0 * X["f0"] + 1.5 * X["f1"] + rng.standard_normal(n) * 0.3
    y = pd.Series((score > 0).astype(int))
    return X, y


# ---------------------------------------------------------------------------
# fixed_horizon_labels
# ---------------------------------------------------------------------------

class TestFixedHorizonLabels:
    def test_output_length(self) -> None:
        from app.lab.quant_ml.labels import fixed_horizon_labels

        prices = _make_prices(50)
        horizon = 5
        labels = fixed_horizon_labels(prices, horizon=horizon)
        assert len(labels) == len(prices) - horizon

    def test_only_valid_label_values(self) -> None:
        from app.lab.quant_ml.labels import fixed_horizon_labels

        prices = _make_prices(50)
        labels = fixed_horizon_labels(prices, horizon=3, threshold=0.0)
        assert set(labels).issubset({-1, 0, 1})

    def test_all_ones_for_monotonically_rising(self) -> None:
        from app.lab.quant_ml.labels import fixed_horizon_labels

        prices = [100.0 + i for i in range(20)]
        labels = fixed_horizon_labels(prices, horizon=1, threshold=0.0)
        assert all(lbl == 1 for lbl in labels)

    def test_all_minus_one_for_monotonically_falling(self) -> None:
        from app.lab.quant_ml.labels import fixed_horizon_labels

        prices = [100.0 - i for i in range(20)]
        labels = fixed_horizon_labels(prices, horizon=1, threshold=0.0)
        assert all(lbl == -1 for lbl in labels)

    def test_threshold_creates_neutral_labels(self) -> None:
        from app.lab.quant_ml.labels import fixed_horizon_labels

        # Tiny moves that fall within a large threshold → all zeros
        prices = [100.0 + 0.0001 * i for i in range(20)]
        labels = fixed_horizon_labels(prices, horizon=1, threshold=0.5)
        assert all(lbl == 0 for lbl in labels)

    def test_horizon_equals_length_gives_empty(self) -> None:
        from app.lab.quant_ml.labels import fixed_horizon_labels

        prices = [100.0, 101.0, 102.0]
        labels = fixed_horizon_labels(prices, horizon=3)
        assert labels == []


# ---------------------------------------------------------------------------
# triple_barrier_labels
# ---------------------------------------------------------------------------

class TestTripleBarrierLabels:
    def test_output_length(self) -> None:
        from app.lab.quant_ml.labels import triple_barrier_labels

        prices = _make_prices(40)
        labels = triple_barrier_labels(prices, time_horizon=10)
        # Implementation iterates range(n - 1)
        assert len(labels) == len(prices) - 1

    def test_only_valid_label_values(self) -> None:
        from app.lab.quant_ml.labels import triple_barrier_labels

        prices = _make_prices(40)
        labels = triple_barrier_labels(prices)
        assert set(labels).issubset({-1, 0, 1})

    def test_time_exit_gives_zero_when_barriers_very_wide(self) -> None:
        """With very wide barriers no barrier is hit → time exit → label 0."""
        from app.lab.quant_ml.labels import triple_barrier_labels

        prices = _make_prices(30)
        labels = triple_barrier_labels(
            prices,
            upper_barrier=100.0,
            lower_barrier=100.0,
            time_horizon=5,
        )
        assert all(lbl == 0 for lbl in labels)

    def test_up_barrier_hit_immediately(self) -> None:
        """If price jumps +10% on bar i+1, label for bar i must be +1."""
        from app.lab.quant_ml.labels import triple_barrier_labels

        prices = [100.0, 110.0, 115.0]
        labels = triple_barrier_labels(
            prices,
            upper_barrier=0.05,
            lower_barrier=0.20,
            time_horizon=5,
        )
        assert labels[0] == 1

    def test_down_barrier_hit_immediately(self) -> None:
        """If price drops -10% on bar i+1, label for bar i must be -1."""
        from app.lab.quant_ml.labels import triple_barrier_labels

        prices = [100.0, 88.0, 90.0]
        labels = triple_barrier_labels(
            prices,
            upper_barrier=0.20,
            lower_barrier=0.05,
            time_horizon=5,
        )
        assert labels[0] == -1

    def test_zero_price_handled_gracefully(self) -> None:
        from app.lab.quant_ml.labels import triple_barrier_labels

        prices = [0.0, 100.0, 102.0]
        labels = triple_barrier_labels(prices)
        assert labels[0] == 0  # p0==0 → append 0 immediately


# ---------------------------------------------------------------------------
# build_features
# ---------------------------------------------------------------------------

class TestBuildFeatures:
    def test_returns_dataframe(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(prices, use_ta=False)
        assert isinstance(result, pd.DataFrame)

    def test_output_has_rows(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(prices, use_ta=False)
        assert len(result) > 0

    def test_no_nulls_in_result(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(prices, use_ta=False)
        assert not result.isnull().any().any()

    def test_lag_columns_present(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(prices, lags=3, use_ta=False)
        for lag in range(1, 4):
            assert f"ret_lag_{lag}" in result.columns

    def test_rolling_vol_columns_present(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(prices, use_ta=False)
        for window in (5, 21, 63):
            assert f"vol_{window}d" in result.columns

    def test_accepts_fifty_row_series(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(50)
        result = build_features(prices, lags=5, use_ta=False)
        # dropna removes leading NaN rows; result should still be non-empty
        assert len(result) > 0

    def test_optional_ta_does_not_crash(self) -> None:
        """use_ta=True should not raise even if pandas_ta is missing."""
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        try:
            result = build_features(prices, use_ta=True)
            assert len(result) > 0
        except Exception:
            pytest.fail("build_features with use_ta=True raised an unexpected exception")

    def test_no_regime_snapshot_omits_regime_columns(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(prices, use_ta=False)
        assert "regime_bull" not in result.columns

    def test_regime_snapshot_adds_regime_columns(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(
            prices, use_ta=False,
            regime_snapshot={"label": "bull", "confidence": 0.8, "vix": 15.0},
        )

        assert result["regime_bull"].iloc[0] == 1.0
        assert result["regime_bear"].iloc[0] == 0.0
        assert result["regime_confidence"].iloc[0] == 0.8
        assert result["regime_crisis"].iloc[0] == 0.0

    def test_regime_snapshot_sets_crisis_above_vix_threshold(self) -> None:
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(
            prices, use_ta=False,
            regime_snapshot={"label": "bear", "confidence": 0.9, "vix": 40.0},
        )

        assert result["regime_crisis"].iloc[0] == 1.0

    def test_malformed_regime_snapshot_degrades_gracefully(self) -> None:
        """A malformed snapshot must not break feature building — the ML
        pipeline should still get its base features, just without regime_*."""
        from app.lab.quant_ml.features import build_features

        prices = _make_prices(60)
        result = build_features(prices, use_ta=False, regime_snapshot="not-a-dict")  # type: ignore[arg-type]

        assert len(result) > 0
        assert "regime_bull" not in result.columns

    def test_extra_features_joins_per_row_not_broadcast(self) -> None:
        """extra_features must vary row-to-row like a real per-date join --
        not get stamped as one constant across every row, which is exactly
        the regime_snapshot incident (see augment_with_regime_features'
        warning docstring): a scalar broadcast is a zero-variance column at
        fit time and a different constant at predict time."""
        from app.lab.quant_ml.features import build_features

        n = 300
        dates = pd.bdate_range("2023-01-01", periods=n).strftime("%Y-%m-%d").tolist()
        prices = _make_prices(n)
        extra = pd.DataFrame(
            {"my_signal": np.arange(n, dtype=float)}, index=pd.DatetimeIndex(dates)
        )

        result = build_features(prices, dates, use_ta=False, extra_features=extra)

        assert "my_signal" in result.columns
        assert len(result) > 1
        assert result["my_signal"].nunique() > 1

    def test_extra_features_nan_does_not_drop_rows(self) -> None:
        """A sparse extra_features column (e.g. IBES SUE, populated only on
        the earnings-announcement date) must not shrink the row count via
        the existing blanket dropna -- imputation happens downstream in the
        sklearn Pipeline instead (see build_pipeline's SimpleImputer)."""
        from app.lab.quant_ml.features import build_features

        n = 300
        dates = pd.bdate_range("2023-01-01", periods=n).strftime("%Y-%m-%d").tolist()
        prices = _make_prices(n)
        without_extra = build_features(prices, dates, use_ta=False)

        sparse = pd.DataFrame(
            {"sue": [float("nan")] * (n - 1) + [2.3]}, index=pd.DatetimeIndex(dates)
        )
        with_extra = build_features(prices, dates, use_ta=False, extra_features=sparse)

        assert len(with_extra) == len(without_extra)
        assert "sue" in with_extra.columns
        assert with_extra["sue"].notna().sum() == 1
        assert with_extra["sue"].iloc[-1] == 2.3

    def test_no_extra_features_reproduces_prior_behavior(self) -> None:
        """extra_features=None (the default) is byte-for-byte the pre-Phase-5
        shape -- no new columns, no NaN introduced."""
        from app.lab.quant_ml.features import build_features

        n = 300
        dates = pd.bdate_range("2023-01-01", periods=n).strftime("%Y-%m-%d").tolist()
        prices = _make_prices(n)

        baseline = build_features(prices, dates, use_ta=False)
        explicit_none = build_features(prices, dates, use_ta=False, extra_features=None)

        pd.testing.assert_frame_equal(baseline, explicit_none)
        assert not explicit_none.isnull().any().any()


# ---------------------------------------------------------------------------
# build_pipeline + train_pipeline (lgbm)
# ---------------------------------------------------------------------------

class TestBuildPipeline:
    def test_lgbm_pipeline_smoke(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline

        X, y = _make_synthetic_xy(n=200, n_features=6, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 10})
        metrics = train_pipeline(pipeline, X, y)

        assert "train_accuracy" in metrics
        assert "train_f1" in metrics
        assert "n_samples" in metrics
        assert metrics["n_samples"] == len(y)
        assert 0.0 <= metrics["train_accuracy"] <= 1.0

    def test_mlp_pipeline_smoke(self) -> None:
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline

        X, y = _make_synthetic_xy(n=200, n_features=6, seed=42)
        pipeline = build_pipeline("mlp", {"max_iter": 10, "hidden_layers": (16,)})
        metrics = train_pipeline(pipeline, X, y)

        assert "train_accuracy" in metrics
        assert 0.0 <= metrics["train_accuracy"] <= 1.0

    def test_unknown_kind_raises_value_error(self) -> None:
        from app.lab.quant_ml.pipelines import build_pipeline

        with pytest.raises(ValueError, match="Unknown model kind"):
            build_pipeline("gpt4", {})

    def test_predict_pipeline_returns_list_of_ints(self) -> None:
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline, predict_pipeline

        pytest.importorskip("lightgbm")
        X, y = _make_synthetic_xy(n=100, n_features=4, seed=7)
        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        train_pipeline(pipeline, X, y)
        preds = predict_pipeline(pipeline, X)
        assert isinstance(preds, list)
        assert all(isinstance(p, int) for p in preds)

    def test_torch_skipped_without_torch(self) -> None:
        """Torch-based models require pytorch; skip if not present."""
        torch = pytest.importorskip("torch")
        # If torch IS present we just verify no crash; if absent the skip happens.
        assert torch is not None

    def test_imputer_step_present(self) -> None:
        from app.lab.quant_ml.pipelines import build_pipeline

        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        assert [name for name, _ in pipeline.steps][0] == "imputer"

    def test_nan_columns_fit_and_predict_without_error(self) -> None:
        """A sparse extra-feature column (e.g. IBES SUE, mostly NaN by
        design) must not blow up StandardScaler -- the new SimpleImputer
        step should absorb it before the scaler ever sees it. Same fitted
        imputer state is reused at predict time, so a row that's entirely
        NaN for the extra column still predicts instead of raising."""
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, predict_pipeline, train_pipeline

        X, y = _make_synthetic_xy(n=200, n_features=6, seed=42)
        X = X.copy()
        X["sparse_signal"] = float("nan")
        X.loc[X.index[-1], "sparse_signal"] = 1.0  # one real value, rest NaN

        pipeline = build_pipeline("lgbm", {"n_estimators": 10})
        metrics = train_pipeline(pipeline, X, y)
        assert 0.0 <= metrics["train_accuracy"] <= 1.0

        # Predict on an all-NaN row for that column (the live-serve case for
        # a ticker with zero coverage from that source at all).
        live_row = X.iloc[[-1]].copy()
        live_row["sparse_signal"] = float("nan")
        preds = predict_pipeline(pipeline, live_row)
        assert isinstance(preds, list)
        assert len(preds) == 1


class TestLabelAlignment:
    def test_align_labels_to_feature_index(self) -> None:
        """Labels must align with feature DataFrame index, not positional."""
        from app.lab.quant_ml.features import build_features
        from app.lab.quant_ml.labels import fixed_horizon_labels
        import pandas as pd

        n = 30
        prices = [100.0 + i * 0.5 for i in range(n)]
        dates = [f"2020-01-{i+1:02d}" for i in range(n)]

        X_df = build_features(prices, dates, use_ta=False)
        assert X_df.index[0] > pd.Timestamp(dates[0])

        horizon = 5
        labels_raw = fixed_horizon_labels(prices, horizon=horizon)

        labels_index = pd.to_datetime(dates[:len(labels_raw)])
        y_raw = pd.Series(labels_raw, index=labels_index)
        y = y_raw.reindex(X_df.index)
        mask = y.notna()
        y = y[mask].astype(int)
        X_aligned = X_df.loc[mask]

        assert y.index.equals(X_aligned.index)

        date_to_price_idx = {d: i for i, d in enumerate(dates)}
        for date, label in y.items():
            i = date_to_price_idx[str(date.date())]
            if i + horizon < len(prices):
                ret = prices[i + horizon] / prices[i] - 1
                expected = 1 if ret > 0 else (-1 if ret < 0 else 0)
                assert label == expected, f"Label mismatch at {date}"

    def test_triple_barrier_alignment(self) -> None:
        """Triple barrier labels also align correctly."""
        from app.lab.quant_ml.features import build_features
        from app.lab.quant_ml.labels import triple_barrier_labels
        import pandas as pd
        import datetime as _dt

        n = 40
        rng = np.random.default_rng(42)
        prices = (100.0 + np.cumsum(rng.normal(0, 1, n))).tolist()
        base = _dt.date(2020, 1, 1)
        dates = [(base + _dt.timedelta(days=i)).isoformat() for i in range(n)]

        X_df = build_features(prices, dates, use_ta=False)
        labels_raw = triple_barrier_labels(prices, upper_barrier=0.02, lower_barrier=0.02, time_horizon=10)

        labels_index = pd.to_datetime(dates[:len(labels_raw)])
        y_raw = pd.Series(labels_raw, index=labels_index)
        y = y_raw.reindex(X_df.index)
        mask = y.notna()
        y = y[mask].astype(int)
        X_aligned = X_df.loc[mask]

        assert y.index.equals(X_aligned.index)
        assert set(y.unique()).issubset({-1, 0, 1})


# ---------------------------------------------------------------------------
# Bug 1: train_pipeline holdout split
# ---------------------------------------------------------------------------

class TestTrainPipelineHoldout:
    def test_holdout_metrics_present(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline

        X, y = _make_synthetic_xy(n=200, n_features=6, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 10})
        metrics = train_pipeline(pipeline, X, y)

        assert "holdout_accuracy" in metrics
        assert "holdout_f1" in metrics
        assert "holdout_samples" in metrics
        assert metrics["holdout_samples"] > 0
        assert 0.0 <= metrics["holdout_accuracy"] <= 1.0
        assert 0.0 <= metrics["holdout_f1"] <= 1.0

    def test_holdout_samples_is_20_percent(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline

        X, y = _make_synthetic_xy(n=200, n_features=6, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 10})
        metrics = train_pipeline(pipeline, X, y, holdout_ratio=0.2)

        assert metrics["holdout_samples"] == 40
        assert metrics["n_samples"] == 200

    def test_holdout_accuracy_lower_than_train(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline

        rng = np.random.default_rng(99)
        X = pd.DataFrame(
            rng.standard_normal((300, 6)),
            columns=[f"f{i}" for i in range(6)],
        )
        y = pd.Series(rng.choice([-1, 0, 1], size=300, p=[0.33, 0.34, 0.33]))

        pipeline = build_pipeline("lgbm", {"n_estimators": 50})
        metrics = train_pipeline(pipeline, X, y)

        assert metrics["train_accuracy"] >= metrics["holdout_accuracy"]

    def test_backward_compatible_keys(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline

        X, y = _make_synthetic_xy(n=100, n_features=4, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        metrics = train_pipeline(pipeline, X, y)

        for key in ("train_accuracy", "train_f1", "n_samples", "n_features"):
            assert key in metrics


# ---------------------------------------------------------------------------
# Bug 2: walk_forward_cv returns fitted pipeline
# ---------------------------------------------------------------------------

class TestWalkForwardFittedPipeline:
    def test_returns_fitted_pipeline(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, walk_forward_cv

        X, y = _make_signal_xy(n=400, n_features=4, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        result = walk_forward_cv(pipeline, X, y)

        assert result["status"] == "completed"
        assert "fitted_pipeline" in result
        fitted = result["fitted_pipeline"]
        assert fitted is not None
        preds = fitted.predict(X.iloc[:10])
        assert len(preds) == 10

    def test_original_pipeline_unfitted(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, walk_forward_cv

        X, y = _make_signal_xy(n=400, n_features=4, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        walk_forward_cv(pipeline, X, y)

        from sklearn.exceptions import NotFittedError
        with pytest.raises(NotFittedError):
            pipeline.predict(X.iloc[:5])

    def test_api_uses_fitted_pipeline(self) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, walk_forward_cv

        X, y = _make_signal_xy(n=400, n_features=4, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        cv_result = walk_forward_cv(pipeline, X, y)

        assert cv_result["status"] == "completed"
        fitted = cv_result.get("fitted_pipeline")
        assert fitted is not None
        fitted_preds = fitted.predict(X.iloc[:10])
        assert len(fitted_preds) == 10

    def test_rejects_pipeline_that_cannot_beat_baseline(self) -> None:
        """Pure-noise features/labels: OOS accuracy should hover near the
        majority-class baseline, so the gate withholds fitted_pipeline."""
        pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, walk_forward_cv

        X, y = _make_synthetic_xy(n=400, n_features=4, seed=42)
        y = pd.Series(np.where(y >= 0, 1, 0))  # binary, still unrelated to X
        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        result = walk_forward_cv(pipeline, X, y)

        assert result["status"] == "rejected_below_baseline"
        assert result["fitted_pipeline"] is None
        assert result["oos_accuracy"] < result["baseline_accuracy"] + 0.05


# ---------------------------------------------------------------------------
# Bug 3: Safe unpickler in registry
# ---------------------------------------------------------------------------

class TestSafeUnpickler:
    def test_rejects_non_pipeline_object(self, tmp_path: Any) -> None:
        import joblib
        from app.lab.quant_ml.registry import load_model

        payload = {"not": "a pipeline"}
        path = tmp_path / "model" / "pipeline.joblib"
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(payload, path)

        with pytest.raises(ValueError, match="not a sklearn Pipeline"):
            load_model("model", base=str(tmp_path))

    def test_rejects_unsafe_pipeline_step(self, tmp_path: Any) -> None:
        from unittest.mock import MagicMock
        from app.lab.quant_ml.registry import _validate_pipeline
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        mock_step = MagicMock()
        mock_step.__class__.__module__ = "malicious.evil"
        type(mock_step).__module__ = "malicious.evil"
        p = Pipeline([("sc", StandardScaler()), ("bad", mock_step)])

        with pytest.raises(ValueError, match="unsafe module"):
            _validate_pipeline(p)

    def test_save_load_roundtrip(self, tmp_path: Any) -> None:
        lgbm = pytest.importorskip("lightgbm")
        from app.lab.quant_ml.pipelines import build_pipeline, train_pipeline
        from app.lab.quant_ml.registry import save_model, load_model

        X, y = _make_synthetic_xy(n=100, n_features=4, seed=42)
        pipeline = build_pipeline("lgbm", {"n_estimators": 5})
        train_pipeline(pipeline, X, y)

        base = str(tmp_path)
        save_model(pipeline, "test_model", base=base)
        loaded = load_model("test_model", base=base)

        preds_original = pipeline.predict(X)
        preds_loaded = loaded.predict(X)
        np.testing.assert_array_equal(preds_original, preds_loaded)
