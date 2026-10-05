"""Spike tests for the jumpmodels regime prototype (ADR-0004).

Tests that exercise the real upstream package are gated on the dev extra
being installed (``pip install -e '.[dev]'`` pins jumpmodels==0.1.1); the
absence-behaviour test runs unconditionally because the lazy-import
RuntimeError contract must hold whether or not the package is present.

Persistence goes through the shared quant-ml registry (joblib files), so
these tests use ``tmp_path`` and never touch the database.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.lab.regime.hmm_model import LABELS, RegimeHMM
from app.lab.regime.jump_model import JumpRegimeModel


def _jumpmodels_available() -> bool:
    try:
        import jumpmodels  # noqa: F401
    except ImportError:
        return False
    return True


_requires_jumpmodels = pytest.mark.skipif(
    not _jumpmodels_available(), reason="jumpmodels dev-extra not installed"
)


def _synthetic_block_features(
    seed: int = 7,
    blocks: tuple[tuple[str, int, float, float], ...] = (
        ("bull", 300, 0.0015, 0.005),
        ("sideways", 300, 0.0000, 0.010),
        ("bear", 300, -0.0030, 0.025),
    ),
) -> tuple[pd.DataFrame, list[str]]:
    """Build production-shaped regime features over distinct synthetic blocks.

    Mirrors ``build_regime_features`` (ret / rolling vol / rolling-peak
    drawdown) so both models see the feature geometry they would see live.
    Returns the feature frame and the per-row ground-truth labels.
    """
    rng = np.random.default_rng(seed)
    rets, truth = [], []
    for name, n, drift, vol in blocks:
        rets.append(rng.normal(drift, vol, n))
        truth.extend([name] * n)

    close = 100.0 * np.cumprod(1.0 + np.concatenate(rets))
    df = pd.DataFrame({"close": close})
    df["ret"] = df["close"].pct_change()
    df["vol"] = df["ret"].rolling(21).std()
    df["drawdown"] = df["close"] / df["close"].rolling(63, min_periods=1).max() - 1
    df = df.dropna().reset_index(drop=True)

    n_dropped = len(truth) - len(df)
    features = df[["ret", "vol", "drawdown"]]
    return features, truth[n_dropped:]


@_requires_jumpmodels
def test_interface_parity_synthetic_blocks() -> None:
    """Labels span ≥2 states; classify_latest matches the shared contract."""
    features, _truth = _synthetic_block_features()
    model = JumpRegimeModel(random_state=42).fit(features)

    labels = model.predict(features)
    assert set(labels) <= set(LABELS)
    assert len(set(labels)) >= 2

    result = model.classify_latest(features)
    assert result["label"] in set(LABELS)
    assert 0.0 <= result["score"] <= 1.0
    assert set(result["probs"]) == set(LABELS)
    assert result["probs"][result["label"]] == pytest.approx(result["score"], abs=1e-9)
    assert sum(result["probs"].values()) == pytest.approx(1.0, abs=1e-6)


@_requires_jumpmodels
def test_save_load_roundtrip(tmp_path: Path) -> None:
    """A loaded model reproduces the fitted model's classify_latest output."""
    features, _truth = _synthetic_block_features(seed=11)
    model = JumpRegimeModel(random_state=42).fit(features)

    path = model.save("spike-jump-roundtrip", base=str(tmp_path))
    loaded = JumpRegimeModel.load("spike-jump-roundtrip", base=str(tmp_path))

    assert path.endswith("pipeline.joblib")
    original = model.classify_latest(features)
    restored = loaded.classify_latest(features)
    assert restored["label"] == original["label"]
    assert restored["score"] == pytest.approx(original["score"], abs=1e-9)
    for label in LABELS:
        assert restored["probs"][label] == pytest.approx(original["probs"][label], abs=1e-9)


@_requires_jumpmodels
def test_determinism_same_seed() -> None:
    """Two fits with the same seed produce identical label sequences."""
    features, _truth = _synthetic_block_features(seed=13)
    labels_a = JumpRegimeModel(random_state=42).fit(features).predict(features)
    labels_b = JumpRegimeModel(random_state=42).fit(features).predict(features)
    assert labels_a == labels_b


@_requires_jumpmodels
def test_parity_with_incumbent_hmm_contract() -> None:
    """Both models accept the same frames and emit the same output shape."""
    features, _truth = _synthetic_block_features(seed=17)
    jump = JumpRegimeModel(random_state=42).fit(features)
    hmm = RegimeHMM(random_state=42, n_init=1).fit(features)

    for model in (jump, hmm):
        result = model.classify_latest(features)
        assert result["label"] in set(LABELS)
        assert sum(result["probs"].values()) == pytest.approx(1.0, abs=1e-6)
        assert model.feature_columns == ["ret", "vol", "drawdown"]
        assert isinstance(model.converged, bool)


def test_absence_raises_clear_runtimeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the package, methods raise a RuntimeError naming the fix.

    Not skipif-gated: this contract must hold regardless of installation.
    """
    import app.lab.regime.jump_model as jump_module

    def _absent() -> object:
        raise ImportError("No module named 'jumpmodels'")

    monkeypatch.setattr(jump_module, "_import_jump_model", _absent)

    model = JumpRegimeModel()  # constructor must not require the package
    features, _truth = _synthetic_block_features()

    with pytest.raises(RuntimeError, match=r"jumpmodels.*not installed"):
        model.fit(features)


@_requires_jumpmodels
def test_score_is_the_calibrated_reliability_of_the_online_label() -> None:
    """The score used to be softmax(-V) over the online DP values, which sits
    at ~1.0 on almost every day (other states trail by about the jump
    penalty). It is now the fitted probability that today's online label is
    the one the model assigns in hindsight, per run-length bucket."""
    features, _truth = _synthetic_block_features(seed=3)
    model = JumpRegimeModel(random_state=42, n_init=1).fit(features)

    table = model._reliability
    assert table is not None and table.shape == (3, 3, 3)
    assert np.allclose(table.sum(axis=2), 1.0)

    result = model.classify_latest(features)
    assert result["score_basis"] == "label_reliability"
    assert result["label"] == model.predict_online(features)[-1]
    row = table[JumpRegimeModel._run_bucket(result["run_length"]), list(LABELS).index(result["label"])]
    assert result["score"] == pytest.approx(row[list(LABELS).index(result["label"])])
    assert sum(result["probs"].values()) == pytest.approx(1.0)
    # Laplace smoothing keeps every entry off 0 and 1.
    assert 0.0 < result["score"] < 1.0


def test_run_length_buckets() -> None:
    labels = np.array(["bull"] * 3 + ["bear"] * 25)
    runs = JumpRegimeModel._run_lengths(labels)
    assert runs[2] == 2 and runs[3] == 0 and runs[-1] == 24
    assert [JumpRegimeModel._run_bucket(r) for r in (0, 4, 5, 20, 21, 300)] == [0, 0, 1, 1, 2, 2]


@_requires_jumpmodels
def test_artefact_without_reliability_scores_with_the_dp_softmax(tmp_path: Path) -> None:
    """A model saved before the reliability table existed still loads; it
    scores with the old softmax until the weekly refit replaces it."""
    features, _truth = _synthetic_block_features(seed=5)
    model = JumpRegimeModel(random_state=42, n_init=1).fit(features)
    model._reliability = None
    model.save("jump-v1", base=str(tmp_path))

    loaded = JumpRegimeModel.load("jump-v1", base=str(tmp_path))
    result = loaded.classify_latest(features)
    assert result["score_basis"] == "dp_softmax"
    assert result["label"] == model.predict_online(features)[-1]
