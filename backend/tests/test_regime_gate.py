"""Tests for regime gate — verifies neutral fallback and weight resolution."""

from app.lab.regime.gate import (
    DEFAULT_REGIME_LABEL,
    REGIME_FACTOR_AFFINITY,
    get_factor_weights,
    get_regime_label,
)


def test_neutral_key_exists_in_affinity_table():
    """DEFAULT_REGIME_LABEL ('neutral') must be a valid key in REGIME_FACTOR_AFFINITY."""
    assert DEFAULT_REGIME_LABEL in REGIME_FACTOR_AFFINITY


def test_neutral_weights_are_identity():
    """Neutral weights should be identity (all 1.0) — every factor treated equally."""
    weights = REGIME_FACTOR_AFFINITY["neutral"]
    assert all(v == 1.0 for v in weights.values())


def test_get_factor_weights_for_valid_labels():
    """Every known regime label should resolve to its own weight table."""
    for label in REGIME_FACTOR_AFFINITY:
        result = get_factor_weights(label)
        assert result == REGIME_FACTOR_AFFINITY[label]


def test_get_factor_weights_for_unknown_label():
    """An unknown label should fall back to the neutral (DEFAULT_REGIME_LABEL) weights."""
    result = get_factor_weights("nonexistent_label")
    assert result == REGIME_FACTOR_AFFINITY[DEFAULT_REGIME_LABEL]


def test_get_regime_label_none_snapshot():
    """None snapshot should return the default label."""
    assert get_regime_label(None) == DEFAULT_REGIME_LABEL


def test_get_regime_label_empty_dict():
    """Empty dict should return the default label."""
    assert get_regime_label({}) == DEFAULT_REGIME_LABEL


def test_get_regime_label_unknown_label():
    """Unrecognised label in snapshot should fall back to default."""
    assert get_regime_label({"label": "apocalyptic"}) == DEFAULT_REGIME_LABEL


def test_get_regime_label_valid_labels():
    """Known labels should pass through unchanged."""
    for label in ["bull", "bear", "high_vol", "low_vol", "sideways", "transition"]:
        assert get_regime_label({"label": label}) == label
