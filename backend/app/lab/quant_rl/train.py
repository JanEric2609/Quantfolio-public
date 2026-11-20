"""FinRL + Stable-Baselines3 training loop."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ALGO_MAP = {
    "ppo": ("stable_baselines3", "PPO"),
    "a2c": ("stable_baselines3", "A2C"),
    "ddpg": ("stable_baselines3", "DDPG"),
    "td3": ("stable_baselines3", "TD3"),
}


def train_policy(
    db: Any,
    policy_id: str,
    env_id: str,
    algo: str = "ppo",
    total_timesteps: int = 10_000,
    params: dict | None = None,
    model_dir: str = "/opt/quantfolio/rl-policies",
    *,
    ohlcv_df: Any,
) -> None:
    """Run RL training. Intended to be called from a background job.

    ``ohlcv_df`` is an already-fetched OHLCV DataFrame (see
    ``app.foundation.market.ohlcv_frame``) — this module never fetches market
    data itself; see ``quant_rl/envs.py``'s module docstring for why.
    """
    from app.foundation.models.entities import QuantRlPolicy

    row: QuantRlPolicy | None = db.get(QuantRlPolicy, policy_id)
    if row is None:
        log.error("QuantRlPolicy %s not found", policy_id)
        return

    try:
        from app.lab.quant_rl.envs import build_env

        row.status = "training"
        db.commit()

        env = build_env(env_id, ohlcv_df)

        module_name, cls_name = _ALGO_MAP.get(algo.lower(), ("stable_baselines3", "PPO"))
        import importlib
        mod = importlib.import_module(module_name)
        AlgoCls = getattr(mod, cls_name)

        model = AlgoCls("MlpPolicy", env, verbose=0, **(params or {}))
        model.learn(total_timesteps=total_timesteps)

        save_path = Path(model_dir) / policy_id
        save_path.mkdir(parents=True, exist_ok=True)
        model.save(str(save_path / "policy"))

        row.artefact_path = str(save_path / "policy.zip")
        row.status = "completed"
        row.training_metrics_json = json.dumps({"total_timesteps": total_timesteps})
        row.finished_at = datetime.now(UTC)

    except Exception as exc:
        log.exception("RL training failed for policy %s", policy_id)
        row.status = "failed"
        row.error_message = str(exc)
        row.finished_at = datetime.now(UTC)
    finally:
        db.commit()
