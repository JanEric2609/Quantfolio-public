"""Vectorbt-based backtest engine for AlphaCrafter Trader (Phase 4).

Produces consistent results with a strategy-as-code runner.

Regime-aware:
  When a ``RegimeContext`` is attached to the ``BacktestSpec``, the engine
  adjusts position sizing and suppresses momentum strategies in adverse
  regimes (bear, high_vol, sideways).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.lab.backtest_vbt.metrics import compute_metrics

# NOTE: ``vectorbt`` is imported lazily inside ``run_backtest`` rather than at
# module top. Its import pulls in numba and thousands of files (~minutes of cold
# I/O on networked/Windows mounts), so deferring it keeps importing the
# AlphaCrafter package — and FastAPI/worker startup — cheap until a backtest
# actually runs.

SignalMode = Literal["binary", "weight"]


@dataclass
class BacktestSpec:
    """Specification for a vectorbt backtest run."""

    symbols: list[str]
    start_date: datetime
    end_date: datetime
    initial_cash: float = 100000.0
    weights: dict[str, float] | None = None
    rebalance_freq: str = "D"
    commission: float = 0.001
    slippage: float = 0.0005
    seed: int | None = None
    regime_context: Any | None = None
    regime_override: dict[str, Any] | None = None
    signal_mode: SignalMode = "binary"


@dataclass
class BacktestResult:
    """Result of a vectorbt backtest."""

    total_return: float
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    trades: int
    final_equity: float
    equity_curve: np.ndarray
    timestamps: np.ndarray
    regime_params: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


async def run_backtest(
    spec: BacktestSpec,
    prices_df: pd.DataFrame,
    entries: pd.DataFrame | None = None,
    exits: pd.DataFrame | None = None,
    weights_df: pd.DataFrame | None = None,
) -> BacktestResult:
    """Run a vectorbt backtest on provided price data.

    Args:
        spec: Backtest specification (symbols, dates, parameters)
        prices_df: DataFrame with datetime index and OHLCV columns
        entries: Boolean DataFrame for entry signals (binary mode only)
        exits: Boolean DataFrame for exit signals (binary mode only)
        weights_df: Continuous weight DataFrame (weight mode only)

    Returns:
        BacktestResult with Sharpe, max drawdown, returns, and equity curve
    """
    try:
        import vectorbt as vbt

        if prices_df.empty:
            return BacktestResult(
                total_return=0.0,
                annual_return=0.0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                win_rate=0.0,
                trades=0,
                final_equity=spec.initial_cash,
                equity_curve=np.array([spec.initial_cash]),
                timestamps=np.array([spec.start_date]),
                error="No price data provided",
            )

        if prices_df.shape[1] == 1:
            close_prices = prices_df.iloc[:, 0]
        else:
            close_prices = prices_df

        # Extract regime-aware parameters (position scaling)
        regime_params: dict[str, Any] = {}
        cash = spec.initial_cash
        if spec.regime_context is not None:
            bp = getattr(spec.regime_context, "backtest_params", None)
            if bp is None and isinstance(spec.regime_context, dict):
                bp = spec.regime_context.get("backtest_params")
            if bp is not None:
                regime_params = dict(bp)
                position_scale = bp.get("position_scale", 1.0)
                cash = cash * position_scale
        if spec.regime_override is not None:
            # Allow explicit override to take precedence
            regime_params.update(spec.regime_override)
            if "position_scale" in spec.regime_override:
                cash = spec.initial_cash * spec.regime_override["position_scale"]

        # Equal-weight rebalancing if no specific weights provided
        if spec.weights is None:
            spec.weights = {s: 1.0 / len(spec.symbols) for s in spec.symbols}

        if spec.signal_mode == "weight" and weights_df is not None:
            portfolio = vbt.Portfolio.from_orders(
                close=close_prices,
                size=weights_df,
                init_cash=cash,
                fees=spec.commission,
                slippage=spec.slippage,
                freq=spec.rebalance_freq,
            )
        else:
            if entries is not None and exits is not None:
                entries_arr = entries.reindex(close_prices.index, fill_value=False)
                exits_arr = exits.reindex(close_prices.index, fill_value=False)
            else:
                entries_arr = np.ones_like(close_prices, dtype=bool)
                exits_arr = np.zeros_like(close_prices, dtype=bool)

            portfolio = vbt.Portfolio.from_signals(
                close=close_prices,
                entries=entries_arr,
                exits=exits_arr,
                init_cash=cash,
                fees=spec.commission,
                slippage=spec.slippage,
                freq=spec.rebalance_freq,
            )

        # Extract the equity (portfolio value) curve. vectorbt 1.0.0 exposes
        # ``Portfolio.value`` (the older ``equity`` was removed); tolerate either
        # a method or a property.
        value = portfolio.value
        equity_series = value() if callable(value) else value
        equity = np.asarray(equity_series, dtype=float)
        if equity.ndim > 1:
            equity = equity.sum(axis=1)

        # Derive risk/return metrics via the ADR-sanctioned builder (delegates
        # to quant_metrics for Sharpe/Sortino/Calmar/annualisation) rather
        # than a parallel hand-rolled computation — see
        # docs/adr/0003-quant-metrics-conventions.md decision 4.
        timestamps_arr = np.asarray(getattr(equity_series, "index", np.arange(equity.size)))
        computed = compute_metrics(equity, timestamps_arr)
        total_return = computed.total_return
        annual_return = computed.annual_return
        sharpe = computed.sharpe_ratio
        max_dd = computed.max_drawdown

        try:
            trades = int(portfolio.trades.count())
        except Exception:
            trades = 0
        try:
            wr = portfolio.trades.win_rate()
            win_rate = float(wr) if wr is not None and not np.isnan(wr) else 0.0
        except Exception:
            win_rate = 0.0

        return BacktestResult(
            total_return=total_return,
            annual_return=annual_return,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            trades=trades,
            final_equity=float(equity[-1]) if equity.size > 0 else cash,
            equity_curve=equity,
            timestamps=timestamps_arr,
            regime_params=regime_params,
        )

    except ImportError as e:
        # Vectorbt/numba import failure is a hard error — the entire Trader
        # stage should fail fast rather than silently returning zeros.
        raise RuntimeError(
            f"vectorbt import failed (cold-start I/O or missing dependency): {e}"
        ) from e
    except Exception as e:
        return BacktestResult(
            total_return=0.0,
            annual_return=0.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            win_rate=0.0,
            trades=0,
            final_equity=spec.initial_cash,
            equity_curve=np.array([spec.initial_cash]),
            timestamps=np.array([spec.start_date]),
            error=str(e),
        )
