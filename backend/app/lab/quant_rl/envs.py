"""FinRL environment wrappers for portfolio allocation.

Deliberately has zero ``app.services`` imports (only ``app.foundation.models``-tier and
third-party deps): ``quant_rl`` is a foundation-shaped package consumed BY
the decision loop (``advisor``, Track D2), never the reverse, and an edge
from here into any flat ``app.services`` module (e.g. ``market.py``) would
merge ``quant_rl`` into the decision loop's existing dense import-cycle SCC
the moment something in ``advisor`` imports it — confirmed via
``check_no_new_cycles.py``. ``build_env`` therefore takes an already-fetched
OHLCV DataFrame instead of a DB session; callers (the RL Lab API, or
``advisor.rl_training`` for Track D2) fetch it themselves via
``app.foundation.market.ohlcv_frame`` — the same "caller fetches, callee
consumes data" split ``quant_ml/training.py`` established for Track D1b.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

AVAILABLE_ENVS = [
    {
        "id": "portfolio_allocation",
        "name": "Portfolio Allocation (FinRL)",
        "description": "Multi-asset portfolio allocation using FinRL StockPortfolioEnv. "
                       "State: OHLCV + technical indicators. Action: portfolio weights.",
        "assets": ["AAPL", "MSFT", "AMZN", "GOOGL", "META"],
        "observation_space": "price + vol + tech_indicators",
        "action_space": "continuous weights",
    },
    {
        "id": "single_stock",
        "name": "Single-Stock Trading (FinRL)",
        "description": "Single-asset buy/sell/hold environment using FinRL StockTradingEnv.",
        "assets": ["EUNL.DE"],
        "observation_space": "OHLCV + tech_indicators",
        "action_space": "discrete: buy/sell/hold",
    },
]

#: Shared across both envs so FeatureEngineer's output columns match exactly
#: what each env's tech_indicator_list expects — a mismatch here raises a
#: KeyError deep inside FinRL's _initiate_state/_update_state.
_TECH_INDICATORS = ["rsi_30", "macd"]

#: Rolling covariance lookback (trading days) for StockPortfolioEnv's
#: required cov_list feature. 63 (one quarter) rather than FinRL's own
#: tutorial default of 252 (one year) — this app's own walk-forward default
#: (backtest_vbt.walk_forward.test_period_days) uses the same quarterly
#: window, and a caller-configurable build_env(start_date, end_date) window
#: shorter than a year would otherwise raise "not enough data" needlessly.
DEFAULT_COV_LOOKBACK = 63


def list_envs() -> list[dict[str, Any]]:
    return AVAILABLE_ENVS


def _add_covariance_list(df: Any, lookback: int = DEFAULT_COV_LOOKBACK) -> Any:
    """Attach the rolling covariance-matrix column (``cov_list``) that
    ``StockPortfolioEnv`` requires — one covariance matrix per date,
    computed from the trailing ``lookback`` trading days of returns across
    all tickers. Standard FinRL portfolio-allocation preprocessing recipe
    (their NeurIPS 2020 tutorial notebook) — ``StockPortfolioEnv`` itself
    only ever reads ``self.data["cov_list"].values[0]``, it doesn't compute
    this from raw prices.
    """
    df = df.sort_values(["date", "tic"], ignore_index=True)
    df.index = df["date"].factorize()[0]
    unique_dates = df["date"].unique()
    if len(unique_dates) <= lookback:
        raise ValueError(
            f"Not enough trading days ({len(unique_dates)}) for a "
            f"{lookback}-day covariance lookback window — narrow the date "
            f"range or pass a smaller lookback."
        )

    cov_list = []
    for i in range(lookback, len(unique_dates)):
        window = df.loc[i - lookback : i, :]
        price_lookback = window.pivot_table(index="date", columns="tic", values="close")
        returns = price_lookback.pct_change().dropna()
        cov_list.append(returns.cov().values)

    cov_df = pd.DataFrame({"date": unique_dates[lookback:], "cov_list": cov_list})
    merged = df.merge(cov_df, on="date")
    merged = merged.sort_values(["date", "tic"]).reset_index(drop=True)
    merged.index = merged["date"].factorize()[0]
    return merged


def build_env(
    env_id: str,
    ohlcv_df: pd.DataFrame,
    start_date: str = "2018-01-01",
    end_date: str = "2022-12-31",
) -> Any:
    """Build a FinRL gym environment from an already-fetched OHLCV DataFrame.

    ``ohlcv_df`` must already be shaped like ``YahooDownloader``'s output
    (date, open, high, low, close, volume, tic) — see
    ``app.foundation.market.ohlcv_frame``, which callers use to build it (this
    module never fetches data itself; see the module docstring). Raises
    ImportError if FinRL not installed.
    """
    try:
        import finrl  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "FinRL is not installed or FINRL_ENABLED=false. "
            "Install with: pip install finrl"
        ) from exc

    from finrl.meta.preprocessor.preprocessors import FeatureEngineer, data_split

    # Both StockPortfolioEnv.step() and StockTradingEnv.step() unconditionally
    # plt.savefig()/write CSVs to a hardcoded "results/" path relative to
    # cwd on the terminal step (finrl's own tutorial-notebook debug output,
    # not something this app reads) — without this the terminal step raises
    # FileNotFoundError, and training/rollout only ever gets there once
    # training is otherwise working.
    Path("results").mkdir(parents=True, exist_ok=True)

    if env_id == "portfolio_allocation":
        # NOTE: StockPortfolioEnv lives in env_portfolio_allocation.env_portfolio,
        # NOT env_portfolio_optimization.env_portfolio_optimization (that module
        # defines a different class, PortfolioOptimizationEnv).
        portfolio_mod = __import__(
            "finrl.meta.env_portfolio_allocation.env_portfolio",
            fromlist=["StockPortfolioEnv"],
        )
        StockPortfolioEnv = portfolio_mod.StockPortfolioEnv

        tickers = ["AAPL", "MSFT", "AMZN", "GOOGL", "META"]
        df = ohlcv_df
        fe = FeatureEngineer(use_technical_indicator=True, tech_indicator_list=_TECH_INDICATORS)
        df = fe.preprocess_data(df)
        train = data_split(df, start_date, end_date)
        train = _add_covariance_list(train)
        stock_dim = len(tickers)
        env = StockPortfolioEnv(
            df=train,
            stock_dim=stock_dim,
            hmax=100,
            initial_amount=100_000,
            transaction_cost_pct=0.001,
            reward_scaling=1e-1,
            state_space=stock_dim,
            action_space=stock_dim,
            tech_indicator_list=_TECH_INDICATORS,
        )
        # Additive metadata (not read by FinRL itself): lets rollout.py map
        # actions_memory's weight vectors back to ticker symbols. Sorted to
        # match _add_covariance_list's df.sort_values(["date", "tic"]) —
        # the actual column order StockPortfolioEnv iterates over.
        env.tickers = sorted(tickers)
        return env

    if env_id == "single_stock":
        trading_mod = __import__(
            "finrl.meta.env_stock_trading.env_stocktrading",
            fromlist=["StockTradingEnv"],
        )
        StockTradingEnv = trading_mod.StockTradingEnv

        df = ohlcv_df
        fe = FeatureEngineer(use_technical_indicator=True, tech_indicator_list=_TECH_INDICATORS)
        df = fe.preprocess_data(df)
        train = data_split(df, start_date, end_date)
        stock_dim = 1
        # FinRL's state layout: [cash] + [close per stock] + [shares held per
        # stock] + [tech indicators per stock] — see StockTradingEnv._initiate_state.
        state_space = 1 + 2 * stock_dim + len(_TECH_INDICATORS) * stock_dim
        env = StockTradingEnv(
            df=train,
            stock_dim=stock_dim,
            hmax=100,
            initial_amount=100_000,
            num_stock_shares=[0],
            buy_cost_pct=[0.001],
            sell_cost_pct=[0.001],
            reward_scaling=1e-4,
            state_space=state_space,
            action_space=stock_dim,
            tech_indicator_list=_TECH_INDICATORS,
        )
        return env

    raise ValueError(f"Unknown env_id: {env_id}")
