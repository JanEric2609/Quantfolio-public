"""discover/pipeline.py::stage_ml_signal serves the pooled cross-sectional
model (lab.quant_lab.pooled_ml, ADR 0015 ruling #24): the candidate's price
features ranked within today's miner-universe cross-section, its prediction
the percentile of its predicted rank. It never trains and never rejects."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from conftest import _memory_db

from app.decision.discover import pipeline
from app.decision.discover.composite import derive_signals_from_scores
from app.decision.discover.pipeline import stage_ml_signal
from app.foundation.models.entities import QuantMlModel, User
from app.lab.quant_lab import pooled_ml as pooled

UNIVERSE = [f"U{i:02d}" for i in range(40)]


def _closes(symbols: list[str], *, n_days: int = 400, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.linspace(-0.002, 0.002, len(symbols))
    rets = rng.normal(0.0, 0.01, (n_days, len(symbols))) + drift
    idx = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=n_days)
    return pd.DataFrame(100.0 * np.cumprod(1.0 + rets, axis=0), index=idx, columns=symbols)


def _user(db) -> User:
    user = User(id=str(uuid.uuid4()), username="ml-signal-user", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No network: rule-based regime inputs, a synthetic bar panel, and
    fresh per-day caches for every test."""
    extra: dict[str, pd.DataFrame] = {}
    universe_close = _closes(UNIVERSE)

    def build_panel(db, universe, start, end, include_fundamentals=False):
        if len(universe) == 1:
            return {"close": extra[universe[0]]} if universe[0] in extra else {}
        return {"close": universe_close}

    monkeypatch.setattr(pipeline, "alpha_build_panel", build_panel)
    monkeypatch.setattr("app.lab.alphacrafter.universe.MINER_UNIVERSE", UNIVERSE)
    pipeline._EXPOSURE_CACHE.clear()
    pipeline._ML_CACHE.clear()
    yield {"extra": extra, "universe": universe_close}
    pipeline._EXPOSURE_CACHE.clear()
    pipeline._ML_CACHE.clear()


def _pooled_row(db, status: str = "completed", artefact_path: str | None = None) -> QuantMlModel:
    row = QuantMlModel(
        user_id=_user(db).id, name=pooled.POOLED_MODEL_NAME, kind="lgbm", ticker=None,
        status=status, artefact_path=artefact_path, feature_schema_version=pooled.POOLED_SCHEMA_VERSION,
    )
    db.add(row)
    db.commit()
    return row


def _trained(db, tmp_path) -> QuantMlModel:
    """A served pooled model fitted on a planted-drift panel."""
    from app.lab.quant_ml.registry import save_model

    rng = np.random.default_rng(7)
    rets = rng.normal(0.0, 0.01, (600, 60)) + rng.normal(0.0, 0.0015, 60)
    close = pd.DataFrame(100.0 * np.cumprod(1.0 + rets, axis=0), index=pd.bdate_range("2023-01-02", periods=600),
                         columns=[f"T{i}" for i in range(60)])
    model = pooled.fit_served(pooled.long_frame(close))
    row = _pooled_row(db)
    row.artefact_path = save_model(model, row.id, base=str(tmp_path))
    db.commit()
    return row


class TestStageMlSignal:
    def test_no_pooled_model_is_no_opinion_with_regime_context(self):
        db = _memory_db()
        scores, reject = stage_ml_signal(db, "U05")
        assert reject is None
        assert scores["prediction"] is None
        assert scores["ml_status"] == "no_model"
        assert "ml_signal_unavailable" in scores["concerns"]
        assert scores["regime_context"]["regime_label"] == "unknown"  # no jump snapshot stored

    def test_a_rejected_model_is_not_served(self):
        db = _memory_db()
        _pooled_row(db, status="rejected_below_gate")
        scores, reject = stage_ml_signal(db, "U05")
        assert reject is None
        assert scores["prediction"] is None
        assert scores["ml_status"] == "rejected_below_gate"

    def test_per_ticker_models_are_no_longer_read(self):
        db = _memory_db()
        user = _user(db)
        db.add(QuantMlModel(user_id=user.id, name="discover-ml-U05", kind="lgbm", ticker="U05",
                            status="completed", artefact_path="/x/pipeline.joblib", feature_schema_version=1))
        db.commit()
        scores, _ = stage_ml_signal(db, "U05")
        assert scores["prediction"] is None
        assert scores["ml_status"] == "no_model"

    def test_serves_a_percentile_within_the_universe(self, tmp_path):
        db = _memory_db()
        row = _trained(db, tmp_path)
        scores, reject = stage_ml_signal(db, "U39")
        assert reject is None
        assert scores["signal_kind"] == "ml_pooled"
        assert scores["model_id"] == row.id
        assert scores["cross_section"] == 40
        assert 0.0 < scores["prediction"] <= 1.0
        # Strongest drift in the universe, and the model learned persistence.
        assert scores["prediction"] > scores_for(db, "U00")
        assert derive_signals_from_scores({"ml_signal": scores})["ml_signal"] == pytest.approx(scores["prediction"])

    def test_a_candidate_outside_the_universe_joins_the_cross_section(self, tmp_path, _offline):
        db = _memory_db()
        _trained(db, tmp_path)
        _offline["extra"]["NEW"] = _closes(["NEW"], seed=9)
        scores, _ = stage_ml_signal(db, "NEW")
        assert scores["signal_kind"] == "ml_pooled"
        assert scores["cross_section"] == 41

    def test_short_history_is_no_opinion(self, tmp_path, _offline):
        db = _memory_db()
        _trained(db, tmp_path)
        _offline["extra"]["IPO"] = _closes(["IPO"], n_days=120)
        scores, reject = stage_ml_signal(db, "IPO")
        assert reject is None
        assert scores["prediction"] is None
        assert scores["ml_status"] == "short_history"

    def test_a_stale_candidate_is_no_opinion(self, tmp_path, _offline):
        db = _memory_db()
        _trained(db, tmp_path)
        _offline["extra"]["OLD"] = _closes(["OLD"]).iloc[:-30]
        scores, _ = stage_ml_signal(db, "OLD")
        assert scores["prediction"] is None
        assert scores["ml_status"] == "stale_or_small_cross_section"

    def test_a_missing_artefact_degrades_never_rejects(self):
        db = _memory_db()
        _pooled_row(db, artefact_path="/nonexistent/abc/pipeline.joblib")
        scores, reject = stage_ml_signal(db, "U05")
        assert reject is None
        assert scores["prediction"] is None
        assert any("ml_signal_error" in c for c in scores["concerns"])

    def test_model_and_universe_are_loaded_once_per_day(self, tmp_path):
        db = _memory_db()
        _trained(db, tmp_path)
        with patch("app.lab.quant_ml.registry.load_model", wraps=__import__(
            "app.lab.quant_ml.registry", fromlist=["load_model"]).load_model) as load:
            for sym in ("U01", "U02", "U03"):
                stage_ml_signal(db, sym)
        assert load.call_count == 1


def scores_for(db, symbol: str) -> float:
    scores, _ = stage_ml_signal(db, symbol)
    return float(scores["prediction"])


class TestMlSignalCompositeWiring:
    def test_unavailable_ml_signal_is_dropped_not_neutral(self):
        """No validated model -> the signal is excluded from weighting
        entirely, not defaulted to a neutral 0.5 (same discipline as
        fundamentals/sentiment when structurally inapplicable)."""
        scores = {"ml_signal": {"prediction": None, "concerns": ["ml_signal_unavailable"]}}
        signals = derive_signals_from_scores(scores)
        assert "ml_signal" not in signals

    def test_pooled_percentile_is_the_signal(self):
        scores = {"ml_signal": {"prediction": 0.83, "signal_kind": "ml_pooled", "concerns": []}}
        assert derive_signals_from_scores(scores)["ml_signal"] == pytest.approx(0.83)

    # Stored per-ticker classifier outputs (-1/0/+1) keep their mapping.
    def test_positive_prediction_maps_above_neutral(self):
        scores = {"ml_signal": {"prediction": 1, "concerns": []}}
        signals = derive_signals_from_scores(scores)
        assert signals["ml_signal"] > 0.5

    def test_negative_prediction_maps_below_neutral(self):
        scores = {"ml_signal": {"prediction": -1, "concerns": []}}
        signals = derive_signals_from_scores(scores)
        assert signals["ml_signal"] < 0.5

    def test_zero_prediction_maps_to_neutral(self):
        scores = {"ml_signal": {"prediction": 0, "concerns": []}}
        signals = derive_signals_from_scores(scores)
        assert signals["ml_signal"] == 0.5
