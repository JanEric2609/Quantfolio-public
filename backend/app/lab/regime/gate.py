"""Regime gate: maps market regimes to factor affinity weights.

Regime labels come from the jump model (``classifier.classify_and_store``,
read through ``macro_snapshot.get_or_refresh_regime``): bull, sideways, bear,
or unknown (neutral weights). The high_vol / low_vol / transition rows are
the retired rule-based labels, kept so stored history still maps.

Each label has a factor affinity table that adjusts the relative importance of
different factor categories.  This drives regime-aware portfolio optimization
and signal gating.

Provides:
  - RegimeContext dataclass: bundles label, confidence, crisis, weights, and
    derived regime-aware parameters for downstream consumers (optimizer,
    backtester, ML, verification).
  - RegimeGate class: central facade for regime context retrieval and
    constraint generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FACTOR_CATEGORY_MAP: dict[str, str] = {
    "momentum_12_1": "momentum",
    "volatility_21d": "risk",
    "rsi_14": "momentum",
    "sma_ratio_50_200": "trend",
    "volume_21d_mean": "liquidity",
    "value_ep": "value",
    "size_log_mc": "size",
    "quality_roe": "quality",
}

# Keys match the labels actually emitted by macro_snapshot.py and hmm_model.py.
# Economic intent per label:
#   bull      – low vol + positive momentum (expansion-like): favor momentum, trend, size
#   bear      – high vol + negative momentum (crisis-like):   favor risk, quality, liquidity
#   high_vol  – high vol but still rising (contraction-like):  mixed, favor value, quality
#   low_vol   – calm but drifting down (late-cycle):           favor value, quality defensively
#   sideways  – HMM middle state:                              neutral identity weights
#   transition – ambiguous macro signals:                      neutral identity weights
REGIME_FACTOR_AFFINITY: dict[str, dict[str, float]] = {
    "bull": {
        "momentum": 1.2,
        "risk": 0.8,
        "trend": 1.3,
        "liquidity": 1.0,
        "value": 0.9,
        "size": 1.1,
        "quality": 0.9,
    },
    "bear": {
        "momentum": 0.5,
        "risk": 1.5,
        "trend": 0.6,
        "liquidity": 1.4,
        "value": 0.7,
        "size": 0.6,
        "quality": 1.4,
    },
    "high_vol": {
        "momentum": 0.7,
        "risk": 1.3,
        "trend": 0.8,
        "liquidity": 1.2,
        "value": 1.2,
        "size": 0.8,
        "quality": 1.3,
    },
    "low_vol": {
        "momentum": 0.8,
        "risk": 0.9,
        "trend": 0.9,
        "liquidity": 1.0,
        "value": 1.3,
        "size": 0.9,
        "quality": 1.2,
    },
    "sideways": {
        "momentum": 1.0,
        "risk": 1.0,
        "trend": 1.0,
        "liquidity": 1.0,
        "value": 1.0,
        "size": 1.0,
        "quality": 1.0,
    },
    "transition": {
        "momentum": 1.0,
        "risk": 1.0,
        "trend": 1.0,
        "liquidity": 1.0,
        "value": 1.0,
        "size": 1.0,
        "quality": 1.0,
    },
    # Fallback for unknown / unrecognised regime labels.
    # Identity weights (all 1.0) — every factor treated equally.
    "neutral": {
        "momentum": 1.0,
        "risk": 1.0,
        "trend": 1.0,
        "liquidity": 1.0,
        "value": 1.0,
        "size": 1.0,
        "quality": 1.0,
    },
}

DEFAULT_REGIME_LABEL = "neutral"

# VIX level above which we flag crisis regardless of the regime label.
CRISIS_VIX_THRESHOLD = 30.0

# ---------------------------------------------------------------------------
# Regime-aware optimizer constraint presets
# ---------------------------------------------------------------------------
# In crisis / bear regimes, the optimizer gets tighter concentration limits
# to reduce risk.  In bull, limits are relaxed.
REGIME_CONSTRAINTS: dict[str, dict[str, float]] = {
    "bull":      {"min": 0.0, "max": 1.0,  "cash_floor": 0.0},
    "low_vol":   {"min": 0.0, "max": 0.6,  "cash_floor": 0.05},
    "high_vol":  {"min": 0.0, "max": 0.4,  "cash_floor": 0.10},
    "sideways":  {"min": 0.0, "max": 0.5,  "cash_floor": 0.05},
    "transition":{"min": 0.0, "max": 0.45, "cash_floor": 0.05},
    "bear":      {"min": 0.0, "max": 0.3,  "cash_floor": 0.15},
    "neutral":   {"min": 0.0, "max": 0.5,  "cash_floor": 0.0},
}

# Regime-aware backtest parameter adjustments.
REGIME_BACKTEST_PARAMS: dict[str, dict[str, Any]] = {
    "bull":      {"momentum_suppressed": False, "mean_reversion_preferred": False, "position_scale": 1.0},
    "low_vol":   {"momentum_suppressed": False, "mean_reversion_preferred": True,  "position_scale": 0.9},
    "high_vol":  {"momentum_suppressed": True,  "mean_reversion_preferred": True,  "position_scale": 0.7},
    "sideways":  {"momentum_suppressed": True,  "mean_reversion_preferred": True,  "position_scale": 0.8},
    "transition":{"momentum_suppressed": True,  "mean_reversion_preferred": False, "position_scale": 0.8},
    "bear":      {"momentum_suppressed": True,  "mean_reversion_preferred": False, "position_scale": 0.5},
    "neutral":   {"momentum_suppressed": False, "mean_reversion_preferred": False, "position_scale": 1.0},
}

# Regime encoding for ML features — one-hot + confidence scalar.
# This maps each label to a numeric index for the one-hot column.
REGIME_LABEL_INDEX: dict[str, int] = {
    "bull": 0, "bear": 1, "high_vol": 2, "low_vol": 3,
    "sideways": 4, "transition": 5, "neutral": 6,
}


@dataclass
class RegimeContext:
    """Bundled regime state for downstream consumers.

    Attributes:
        label: Normalised regime label (always a key in REGIME_FACTOR_AFFINITY).
        confidence: Classifier confidence in [0, 1], or None if unavailable.
        crisis: Whether crisis conditions are active.
        factor_weights: Regime-conditioned factor affinity weights.
        vix: Latest VIX reading, or None.
        yield_spread: Latest yield curve spread, or None.
        momentum_3m: 3-month world momentum, or None.
        constraints: Optimizer weight constraints derived from regime.
        backtest_params: Backtest parameter adjustments from regime.
    """
    label: str = DEFAULT_REGIME_LABEL
    confidence: float | None = None
    crisis: bool = False
    factor_weights: dict[str, float] = field(default_factory=lambda: dict(REGIME_FACTOR_AFFINITY[DEFAULT_REGIME_LABEL]))
    vix: float | None = None
    yield_spread: float | None = None
    momentum_3m: float | None = None
    constraints: dict[str, float] = field(default_factory=lambda: dict(REGIME_CONSTRAINTS[DEFAULT_REGIME_LABEL]))
    backtest_params: dict[str, Any] = field(default_factory=lambda: dict(REGIME_BACKTEST_PARAMS[DEFAULT_REGIME_LABEL]))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for API responses."""
        return {
            "regime_label": self.label,
            "crisis": self.crisis,
            "score": self.confidence,
            "factor_weights": self.factor_weights,
            "vix": self.vix,
            "yield_spread": self.yield_spread,
            "momentum_3m": self.momentum_3m,
            "constraints": self.constraints,
            "backtest_params": self.backtest_params,
        }

    def regime_features(self) -> dict[str, float]:
        """Return regime state encoded as numeric features for ML models.

        Includes:
          - regime_bull, regime_bear, ..., regime_neutral: one-hot encoding
          - regime_confidence: scalar confidence value
          - regime_crisis: 1.0 if crisis else 0.0
        """
        features: dict[str, float] = {}
        # One-hot encode the label
        idx = REGIME_LABEL_INDEX.get(self.label, REGIME_LABEL_INDEX["neutral"])
        for name, i in REGIME_LABEL_INDEX.items():
            features[f"regime_{name}"] = 1.0 if i == idx else 0.0
        features["regime_confidence"] = self.confidence if self.confidence is not None else 0.5
        features["regime_crisis"] = 1.0 if self.crisis else 0.0
        return features


def get_regime_label(regime_snapshot: dict | None) -> str:
    """Extract and normalise the regime label from a snapshot dict.

    Returns a key that is guaranteed to exist in REGIME_FACTOR_AFFINITY
    (falls back to DEFAULT_REGIME_LABEL for unknown / missing labels).
    """
    if not regime_snapshot or not isinstance(regime_snapshot, dict):
        return DEFAULT_REGIME_LABEL
    label = regime_snapshot.get("label", DEFAULT_REGIME_LABEL)
    if not isinstance(label, str):
        return DEFAULT_REGIME_LABEL
    normalised = label.strip().lower() or DEFAULT_REGIME_LABEL
    return normalised if normalised in REGIME_FACTOR_AFFINITY else DEFAULT_REGIME_LABEL


def get_factor_weights(regime_label: str) -> dict[str, float]:
    return REGIME_FACTOR_AFFINITY.get(regime_label, REGIME_FACTOR_AFFINITY[DEFAULT_REGIME_LABEL])


def apply_regime_gate(
    factor_scores: dict[str, float],
    regime_label: str,
) -> dict[str, Any]:
    weights = get_factor_weights(regime_label)
    adjusted: dict[str, float] = {}
    for factor_name, score in factor_scores.items():
        category = FACTOR_CATEGORY_MAP.get(factor_name, "neutral")
        w = weights.get(category, 1.0)
        adjusted[factor_name] = score * w
    return {
        "adjusted_scores": adjusted,
        "regime_label": regime_label,
        "weights_used": weights,
    }


def _is_crisis(snapshot: dict, label: str) -> bool:
    """Determine crisis status from the snapshot data.

    Crisis fires when either:
      - The VIX reading in the snapshot exceeds CRISIS_VIX_THRESHOLD, or
      - The regime label is 'bear' (high vol + negative momentum).
    """
    if label == "bear":
        return True
    vix = snapshot.get("vix")
    if isinstance(vix, (int, float)) and vix > CRISIS_VIX_THRESHOLD:
        return True
    return False


def get_regime_adjusted_weights(db: Any) -> dict[str, Any]:
    """Legacy function — returns flat dict with regime weights (backward-compat)."""
    from app.lab.regime.macro_snapshot import get_or_refresh_regime

    snapshot = get_or_refresh_regime(db)
    label = get_regime_label(snapshot)
    crisis = _is_crisis(snapshot, label) if isinstance(snapshot, dict) else (label == "bear")
    weights = get_factor_weights(label)
    raw = snapshot if isinstance(snapshot, dict) else {}
    return {
        # "unknown" stays "unknown" for display; the weights are the neutral ones.
        "regime_label": label if label != DEFAULT_REGIME_LABEL else str(raw.get("label") or label),
        "crisis": crisis,
        "factor_weights": weights,
        "score": None,
        "model": raw.get("model"),
        "available": bool(raw.get("available")),
        "state_since": raw.get("state_since"),
        "as_of": raw.get("as_of"),
        "vix": raw.get("vix"),
        "reason": raw.get("reason"),
    }


class RegimeGate:
    """Central facade for regime context retrieval and constraint generation.

    Usage::

        gate = RegimeGate(db)
        ctx = gate.context()            # RegimeContext
        ctx.constraints                 # for optimizer
        ctx.backtest_params             # for backtester
        ctx.regime_features()           # for ML pipeline
    """

    def __init__(self, db: Any):
        self._db = db
        self._ctx: RegimeContext | None = None

    def context(self) -> RegimeContext:
        """Build and cache a RegimeContext from the current regime snapshot."""
        if self._ctx is not None:
            return self._ctx
        from app.lab.regime.macro_snapshot import get_or_refresh_regime

        snapshot = get_or_refresh_regime(self._db)
        label = get_regime_label(snapshot)
        crisis = _is_crisis(snapshot, label) if isinstance(snapshot, dict) else (label == "bear")
        weights = get_factor_weights(label)
        confidence: float | None = snapshot.get("confidence") if isinstance(snapshot, dict) else None
        vix = snapshot.get("vix") if isinstance(snapshot, dict) else None
        yield_spread = snapshot.get("yield_spread") if isinstance(snapshot, dict) else None
        momentum_3m = snapshot.get("momentum_3m") if isinstance(snapshot, dict) else None

        self._ctx = RegimeContext(
            label=label,
            confidence=confidence,
            crisis=crisis,
            factor_weights=weights,
            vix=vix if isinstance(vix, (int, float)) else None,
            yield_spread=yield_spread if isinstance(yield_spread, (int, float)) else None,
            momentum_3m=momentum_3m if isinstance(momentum_3m, (int, float)) else None,
            constraints=dict(REGIME_CONSTRAINTS.get(label, REGIME_CONSTRAINTS["neutral"])),
            backtest_params=dict(REGIME_BACKTEST_PARAMS.get(label, REGIME_BACKTEST_PARAMS["neutral"])),
        )
        return self._ctx

    def optimizer_weights_constraint(self) -> dict[str, float]:
        """Return {'min': ..., 'max': ...} for skfolio optimiser."""
        return {"min": self.context().constraints["min"],
                "max": self.context().constraints["max"]}

    def backtest_params(self) -> dict[str, Any]:
        """Return regime-aware backtest parameter adjustments."""
        return self.context().backtest_params

    def ml_features(self) -> dict[str, float]:
        """Return regime features for the ML pipeline."""
        return self.context().regime_features()
