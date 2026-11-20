"""Perturbation engine for Discovery self-improvement loop (P3c).

Generates slightly variant ``DiscoveryConfig`` rows from a base config
to serve as challenger candidates in the auto-tuning pipeline.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryConfig
from app.decision.discover.config import create_config, get_active_config, get_config
from app.decision.discover.framings import STYLE_FRAMINGS

logger = logging.getLogger(__name__)

PROMPT_VARIATIONS: list[dict[str, Any]] = [
    {"temperature": 0.1, "style": "conservative"},
    {"temperature": 0.3, "style": "aggressive"},
    {"temperature": 0.2, "focus": "fundamentals"},
    {"temperature": 0.2, "focus": "momentum"},
]


def _short_hash(obj: dict[str, Any]) -> str:
    """SHA-256 digest of sorted JSON, truncated to 6 hex chars."""
    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:6]


def _build_prompt(base_prompt: str, variation: dict[str, Any]) -> dict[str, Any]:
    """Wrap a base prompt with the framing line from *variation*.

    Returns a full ``config_json`` dict with the modified ``system_prompt``
    and overridden ``temperature``.
    """
    style_key = variation.get("style") or variation.get("focus", "")
    framing = STYLE_FRAMINGS.get(style_key, "")
    modified = base_prompt.rstrip("\n") + "\n\n" + framing
    return {
        "system_prompt": modified,
        "temperature": variation.get("temperature", 0.2),
        "max_tokens": 4096,
    }


# A draw this close to the parent weights is not a distinguishable challenger.
_MIN_L1_DISTANCE = 0.01

# Redraws allowed per variant before that variant is given up on.  One draw
# lands under the threshold surprisingly often on a small config -- for a
# two-signal config at the default ``noise_std`` the renormalised L1 distance
# is |n0 - n1|, which is under 0.01 about 11% of the time -- so drawing once
# and moving on meant a caller asking for two variants got none roughly once
# in eighty calls.  Redrawing keeps every persisted variant distinguishable
# while making a short return the rare case it reads as.
_MAX_DRAWS_PER_VARIANT = 8


def _draw_distinct_weights(
    rng: np.random.Generator,
    base_array: NDArray[np.float64],
    noise_std: float,
) -> tuple[NDArray[np.float64], float] | None:
    """Draw renormalised weights that differ measurably from *base_array*.

    Returns ``(weights, l1_distance)``, or ``None`` if every attempt landed
    within ``_MIN_L1_DISTANCE`` of the parent (or collapsed to all zeros).
    """
    for _ in range(_MAX_DRAWS_PER_VARIANT):
        noise = rng.normal(0, noise_std, size=base_array.size)
        perturbed = np.clip(base_array + noise, 0.0, 1.0)
        total = float(perturbed.sum())
        if total < 1e-9:
            continue
        perturbed = perturbed / total
        l1 = float(np.sum(np.abs(perturbed - base_array)))
        if l1 >= _MIN_L1_DISTANCE:
            return perturbed, l1
    return None


def generate_weight_perturbations(
    db: Session,
    base_config_id: str | None = None,
    n_variants: int = 5,
    noise_std: float = 0.05,
    seed: int | None = None,
) -> list[DiscoveryConfig]:
    """Add Gaussian noise to signal weights, renormalize, and persist as challenger configs.

    A draw whose L1 distance from the parent weights is below 0.01 is
    near-identical and gets redrawn (up to ``_MAX_DRAWS_PER_VARIANT`` times);
    a variant is only dropped if every redraw is also near-identical, so the
    returned list is normally ``n_variants`` long.

    Args:
        db: Database session.
        base_config_id: Optional parent config.  Falls back to the active
            ``"signal_weights"`` config.
        n_variants: How many perturbed copies to create (default 5).
        noise_std: Standard deviation of the Gaussian noise (default 0.05).
        seed: Seed for the noise generator.  Leave ``None`` in production;
            pass a value to make a run reproducible (tests rely on this).

    Returns:
        List of newly created ``DiscoveryConfig`` rows.
    """
    if base_config_id is not None:
        base = get_config(db, base_config_id)
    else:
        base = get_active_config(db, "signal_weights")

    if base is None:
        logger.warning("No base signal_weights config found — cannot perturb")
        return []

    base_weights: dict[str, float] = base.config_json.get("weights", {})
    base_threshold: float = base.config_json.get("threshold_ic_obs", 20)
    keys = list(base_weights.keys())
    base_array = np.array([base_weights[k] for k in keys], dtype=float)

    created: list[DiscoveryConfig] = []
    rng = np.random.default_rng(seed)

    for _ in range(n_variants):
        drawn = _draw_distinct_weights(rng, base_array, noise_std)
        if drawn is None:
            logger.warning(
                "Dropped a perturbation of config %s: %d draws at noise_std=%s all landed "
                "within %s of the parent weights",
                base.id, _MAX_DRAWS_PER_VARIANT, noise_std, _MIN_L1_DISTANCE,
            )
            continue
        perturbed, l1 = drawn

        new_weights = {k: round(float(v), 4) for k, v in zip(keys, perturbed, strict=False)}
        config_json: dict[str, Any] = {
            "weights": new_weights,
            "threshold_ic_obs": base_threshold,
        }

        cfg = create_config(
            db,
            config_type="signal_weights",
            config_json=config_json,
            version_label=f"perturb-{_short_hash(config_json)}",
            description=f"Perturbation (noise_std={noise_std}, l1={l1:.4f})",
            source="perturbation",
            parent_config_id=base.id,
        )
        created.append(cfg)

    logger.info("Generated %d weight perturbations from config %s", len(created), base.id)
    return created


def generate_prompt_perturbations(
    db: Session,
    base_config_id: str | None = None,
) -> list[DiscoveryConfig]:
    """Generate 4 prompt-template variants from the PROMPT_VARIATIONS list.

    Each variation sets a different style/focus framing and temperature.

    Args:
        db: Database session.
        base_config_id: Optional parent config.  Falls back to the active
            ``"prompt_template"`` config.

    Returns:
        List of newly created ``DiscoveryConfig`` rows.
    """
    if base_config_id is not None:
        base = get_config(db, base_config_id)
    else:
        base = get_active_config(db, "prompt_template")

    if base is None:
        logger.warning("No base prompt_template config found — cannot perturb")
        return []

    base_prompt = base.config_json.get("system_prompt", "")

    created: list[DiscoveryConfig] = []

    for variation in PROMPT_VARIATIONS:
        config_json = _build_prompt(base_prompt, variation)
        cfg = create_config(
            db,
            config_type="prompt_template",
            config_json=config_json,
            version_label=f"prompt-{_short_hash(config_json)}",
            description=f"Prompt variant: {variation.get('style') or variation.get('focus')}",
            source="perturbation",
            parent_config_id=base.id,
        )
        created.append(cfg)

    logger.info("Generated %d prompt perturbations from config %s", len(created), base.id)
    return created
