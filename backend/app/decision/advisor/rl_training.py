"""Train + validate an RL portfolio-allocation policy for a user (Track D2).

Lives in ``advisor``, not ``quant_rl``: fetching OHLCV history needs
``app.foundation.market`` and gating the rollout needs
``app.foundation.quant_metrics.sharpe_ratio``, both decision-loop-entangled
flat ``app.services`` modules. ``quant_rl`` itself must stay a pure
foundation package (train/rollout mechanics only, taking data in as plain
parameters) so that ``advisor`` importing it doesn't merge ``quant_rl``
into the decision loop's existing dense import-cycle SCC — confirmed via
``check_no_new_cycles.py``. This module is where that data-fetching and
gating actually happens: it already sits inside the SCC (``advisor``
already touches ``market``/``quant_metrics`` elsewhere), so importing them
here adds no new cycle. Mirrors ``quant_ml/training.py``'s shared-core
shape (train, gate, always persist — status encodes trust), extended one
layer further out because RL's gate needs a services-tier metric, not just
services-tier data.

Called directly by the scheduled ``advisor_rl_training`` job
(``advisor/jobs.py``) — training is far too slow to run inline in the daily
paper-trading cycle; ``advisor/rl_signal.py`` is what that cycle actually
reads from (only the latest validated policy, never trains on demand).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: Same discipline as quant_ml/training.py's 30-row floor for an aligned
#: (X, y): a rollout shorter than this can't support a meaningful Sharpe
#: estimate.
MIN_PERIOD_RETURNS = 30

_DEFAULT_LOOKBACK_YEARS = 5


def train_and_validate_policy(
    db: Any,
    user_id: str,
    *,
    env_id: str = "portfolio_allocation",
    algo: str = "ppo",
    total_timesteps: int = 10_000,
    n_rollout_steps: int = 252,
    model_dir: str = "/opt/quantfolio/rl-policies",
    lookback_years: int = _DEFAULT_LOOKBACK_YEARS,
) -> Any:
    """Train a fresh policy for *user_id* over a rolling lookback window,
    roll it out, and validate the rollout's period returns via
    ``quant_metrics.sharpe_ratio`` before trusting it. Always persisted as
    a ``QuantRlPolicy`` row — status reflects whether it passed
    (``"completed"``) or not (``"rejected_below_baseline"``/``"failed"``),
    matching ``QuantMlModel``'s status-encodes-trust discipline (Track
    D1b). Never raises: training/rollout/fetch failures are all caught and
    surface as a rejected/failed status instead.

    Returns the persisted ``QuantRlPolicy`` row.
    """
    from app.foundation.models.entities import QuantRlPolicy
    from app.foundation.market import ohlcv_frame
    from app.foundation.quant_metrics import sharpe_ratio
    from app.lab.quant_rl.envs import AVAILABLE_ENVS
    from app.lab.quant_rl.rollout import run_rollout
    from app.lab.quant_rl.train import train_policy

    row = QuantRlPolicy(
        user_id=user_id,
        env_id=env_id,
        algo=algo,
        params_json=json.dumps({"total_timesteps": total_timesteps}),
        status="queued",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    tickers = next((e["assets"] for e in AVAILABLE_ENVS if e["id"] == env_id), [])
    end = date.today()
    start = end - timedelta(days=365 * lookback_years)
    try:
        df = ohlcv_frame(db, tickers, start.isoformat(), end.isoformat())
    except Exception as exc:
        logger.warning("advisor rl_training: OHLCV fetch failed for %s: %s", env_id, exc)
        row.status = "failed"
        row.error_message = f"OHLCV fetch failed: {exc}"
        row.finished_at = datetime.now(UTC)
        db.commit()
        return row

    train_policy(
        db, row.id, env_id, algo=algo, total_timesteps=total_timesteps,
        model_dir=model_dir, ohlcv_df=df,
    )
    db.refresh(row)
    if row.status != "completed":
        # train_policy already recorded "failed" + error_message; nothing
        # to roll out or validate.
        return row

    result = run_rollout(
        db, row.id, env_id, algo=algo, n_steps=n_rollout_steps,
        model_dir=model_dir, ohlcv_df=df,
    )
    period_returns = result.get("period_returns") or []

    metrics: dict[str, Any] = {}
    try:
        metrics = json.loads(row.training_metrics_json or "{}")
    except (TypeError, ValueError):
        metrics = {}
    metrics["rollout_status"] = result.get("status")
    metrics["final_value"] = result.get("final_value")
    metrics["final_weights"] = result.get("final_weights")

    if result.get("status") != "completed" or len(period_returns) < MIN_PERIOD_RETURNS:
        metrics["rollout_sharpe"] = None
        row.training_metrics_json = json.dumps(metrics)
        row.status = "rejected_below_baseline"
        row.finished_at = datetime.now(UTC)
        db.commit()
        logger.info(
            "advisor rl_training: policy %s rejected — rollout status=%s, %d period returns",
            row.id, result.get("status"), len(period_returns),
        )
        return row

    rollout_sharpe = sharpe_ratio(period_returns)
    metrics["rollout_sharpe"] = rollout_sharpe
    row.training_metrics_json = json.dumps(metrics)
    row.status = "completed" if rollout_sharpe > 0.0 else "rejected_below_baseline"
    row.finished_at = datetime.now(UTC)
    db.commit()
    logger.info(
        "advisor rl_training: policy %s -> status=%s (rollout_sharpe=%.4f)",
        row.id, row.status, rollout_sharpe,
    )
    return row
