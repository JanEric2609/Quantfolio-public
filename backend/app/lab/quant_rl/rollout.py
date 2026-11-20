"""Run a trained RL policy and produce an equity curve."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def run_rollout(
    db: Any,
    policy_id: str,
    env_id: str,
    algo: str = "ppo",
    n_steps: int = 252,
    model_dir: str = "/opt/quantfolio/rl-policies",
    *,
    ohlcv_df: Any,
) -> dict[str, Any]:
    """``ohlcv_df`` is an already-fetched OHLCV DataFrame (see
    ``app.foundation.market.ohlcv_frame``) — this module never fetches market
    data itself; see ``quant_rl/envs.py``'s module docstring for why.
    """
    try:
        from app.lab.quant_rl.envs import build_env
        import importlib

        algo_map = {
            "ppo": ("stable_baselines3", "PPO"),
            "a2c": ("stable_baselines3", "A2C"),
            "ddpg": ("stable_baselines3", "DDPG"),
            "td3": ("stable_baselines3", "TD3"),
        }
        module_name, cls_name = algo_map.get(algo.lower(), ("stable_baselines3", "PPO"))
        mod = importlib.import_module(module_name)
        AlgoCls = getattr(mod, cls_name)

        save_path = Path(model_dir) / policy_id / "policy"
        model = AlgoCls.load(str(save_path))

        env = build_env(env_id, ohlcv_df)
        obs, _ = env.reset()
        done = False
        step = 0
        while not done and step < n_steps:
            action, _ = model.predict(obs)
            obs, _reward, terminated, truncated, _ = env.step(action)
            done = terminated or truncated
            step += 1

        # FinRL's StockTradingEnv/StockPortfolioEnv track the true portfolio value
        # in asset_memory. Read it directly. The previous code compounded the raw
        # step reward as if it were a fractional return, but FinRL's reward is a
        # scaled value delta (reward_scaling * Δasset), so the resulting "equity
        # curve" was meaningless (near-flat or wildly wrong).
        asset_memory = getattr(env, "asset_memory", None)
        if asset_memory and len(asset_memory) > 1:
            base = float(asset_memory[0]) or 1.0
            portfolio_values = [float(v) / base for v in asset_memory]
        else:
            portfolio_values = [1.0]

        # Additive: per-ticker final weights and per-step returns, used by
        # advisor/rl_training.py to gate a policy on its own rollout Sharpe
        # and to surface a per-ticker advisory signal. StockPortfolioEnv
        # sorts its training frame by ["date", "tic"]
        # (envs.py::_add_covariance_list), so actions_memory's column order
        # is always alphabetical by ticker — env.tickers (attached in
        # envs.py::build_env) is already sorted to match.
        final_weights: dict[str, float] | None = None
        tickers = getattr(env, "tickers", None)
        actions_memory = getattr(env, "actions_memory", None)
        if tickers and actions_memory:
            last_action = actions_memory[-1]
            if len(last_action) == len(tickers):
                final_weights = {
                    tic: float(w) for tic, w in zip(tickers, last_action, strict=True)
                }

        return_memory = getattr(env, "portfolio_return_memory", None)
        period_returns = [float(r) for r in return_memory[1:]] if return_memory else []

        return {
            "status": "completed",
            "n_steps": step,
            "final_value": portfolio_values[-1],
            "equity_curve": [{"step": i, "value": v} for i, v in enumerate(portfolio_values)],
            "final_weights": final_weights,
            "period_returns": period_returns,
        }
    except Exception as exc:
        return {"status": "failed", "message": str(exc)}
