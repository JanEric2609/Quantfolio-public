"""Metrics computation for backtest results (Phase 4).

Reuses quant_metrics.py for Sharpe, Sortino, etc. where applicable.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.foundation import quant_metrics


@dataclass
class BacktestMetrics:
    """Comprehensive metrics for a backtest result."""

    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    drawdown_duration_days: int
    total_return: float
    annual_return: float
    win_rate: float
    profit_factor: float
    return_std: float


def compute_metrics(
    equity_curve: np.ndarray,
    timestamps: np.ndarray,
    trades: list[dict] | None = None,
    periods_per_year: int = quant_metrics.TRADING_DAYS_PER_YEAR,
) -> BacktestMetrics:
    """Compute comprehensive metrics from equity curve and trades.

    Annualisation convention: **trading days** (252 periods/year) — equity
    curves come from market-data backtests sampled on trading sessions. See
    the convention table in ``docs/adr/0003-quant-metrics-conventions.md``
    (decision 4); ``periods_per_year`` makes that cadence an explicit
    parameter instead of a silent hardcoded 252.

    Args:
        equity_curve: Array of portfolio equity values over time
        timestamps: Array of timestamps corresponding to equity values
        trades: Optional list of trade records with entry/exit prices
        periods_per_year: Cadence used to annualise Sharpe, Sortino, Calmar
            and the compounded annual return. Defaults to
            ``quant_metrics.TRADING_DAYS_PER_YEAR``, preserving the historical
            behaviour exactly.

    Returns:
        BacktestMetrics with risk/return profile

    Raises:
        ValueError: If ``periods_per_year`` is not positive.
    """
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year!r}")

    if len(equity_curve) == 0:
        return BacktestMetrics(
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            calmar_ratio=0.0,
            max_drawdown=0.0,
            drawdown_duration_days=0,
            total_return=0.0,
            annual_return=0.0,
            win_rate=0.0,
            profit_factor=1.0,
            return_std=0.0,
        )

    # Convert to DataFrame for quant_metrics functions
    returns = np.diff(equity_curve) / equity_curve[:-1]
    df_returns = pd.DataFrame({
        "date": timestamps[1:],
        "return": returns,
    })

    # Compute basic metrics via quant_metrics
    sharpe = quant_metrics.sharpe_ratio(df_returns["return"].tolist(), periods_per_year=periods_per_year)
    sortino = quant_metrics.sortino_ratio(df_returns["return"].tolist(), periods_per_year=periods_per_year)
    calmar = quant_metrics.calmar_ratio(df_returns["return"].tolist(), periods_per_year=periods_per_year)

    # Drawdown depth + duration delegate to the canonical implementation
    # (ADR 0003 decision 4: single-source correctness). Duration is the longest
    # consecutive underwater streak (peak-to-peak time between new equity highs),
    # NOT the count of drawdown episodes.
    dd_stats = quant_metrics.max_drawdown(returns.tolist())
    max_dd = float(dd_stats.get("max_drawdown", 0.0))
    dd_duration = int(dd_stats.get("max_drawdown_duration", 0.0))

    # Returns
    if len(equity_curve) >= 2 and equity_curve[0] != 0:
        total_return = (equity_curve[-1] / equity_curve[0]) - 1.0
        n_periods = len(returns)
        years = n_periods / periods_per_year
        annual_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    else:
        total_return = 0.0
        annual_return = 0.0
    return_std = float(np.std(returns)) if len(returns) > 0 else 0.0

    # Win rate (from trades if available, else from returns)
    win_rate = 0.0
    if trades:
        winning_trades = sum(1 for t in trades if t.get("pnl", 0) > 0)
        win_rate = winning_trades / len(trades) if trades else 0.0
    else:
        winning_trades = np.sum(returns > 0)
        win_rate = winning_trades / len(returns) if len(returns) > 0 else 0.0

    # Profit factor
    if trades:
        total_profit = sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) > 0)
        total_loss = abs(sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) < 0))
        profit_factor = total_profit / total_loss if total_loss > 0 else 1.0
    else:
        profit_factor = 1.0

    return BacktestMetrics(
        sharpe_ratio=float(sharpe),
        sortino_ratio=float(sortino),
        calmar_ratio=float(calmar),
        max_drawdown=float(max_dd),
        drawdown_duration_days=dd_duration,
        total_return=float(total_return),
        annual_return=float(annual_return),
        win_rate=float(win_rate),
        profit_factor=float(profit_factor),
        return_std=float(return_std),
    )
