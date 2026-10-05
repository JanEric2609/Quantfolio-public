"""Tests for backtest_vbt/engine.py's Track C0 fixes: slippage was configured
end-to-end but never passed to vectorbt, and run_backtest's metrics were a
hand-rolled duplicate of the ADR-sanctioned compute_metrics builder."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest
from app.lab.backtest_vbt.metrics import compute_metrics


def _make_prices(days: int = 60) -> pd.DataFrame:
    np.random.seed(7)
    dates = pd.date_range("2024-01-01", periods=days, freq="B")
    prices = 100 + np.cumsum(np.random.randn(days) * 0.5 + 0.1)
    return pd.DataFrame({"AAPL": prices}, index=dates)


def _make_binary_signals(prices_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = len(prices_df)
    entries = pd.DataFrame(
        {col: [i % 10 == 0 for i in range(n)] for col in prices_df.columns},
        index=prices_df.index,
    )
    exits = pd.DataFrame(
        {col: [i % 10 == 5 for i in range(n)] for col in prices_df.columns},
        index=prices_df.index,
    )
    return entries, exits


class TestSlippageIsApplied:
    @pytest.mark.asyncio
    async def test_nonzero_slippage_reduces_final_equity_vs_zero_slippage(self):
        """A backtest with real slippage should never outperform the same
        strategy at zero slippage — if slippage silently isn't applied, the
        two runs are numerically identical instead."""
        prices = _make_prices(days=60)
        entries, exits = _make_binary_signals(prices)

        zero_slippage_spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            slippage=0.0,
        )
        high_slippage_spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            slippage=0.05,  # 5% — deliberately large so the effect isn't lost to noise
        )

        zero_result = await run_backtest(zero_slippage_spec, prices, entries=entries, exits=exits)
        high_result = await run_backtest(high_slippage_spec, prices, entries=entries, exits=exits)

        assert zero_result.error is None
        assert high_result.error is None
        assert high_result.trades >= 1
        assert high_result.final_equity < zero_result.final_equity

    @pytest.mark.asyncio
    async def test_weight_mode_slippage_reduces_final_equity(self):
        prices = _make_prices(days=60)
        weights_df = pd.DataFrame({"AAPL": [0.5] * len(prices)}, index=prices.index)

        zero_slippage_spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            signal_mode="weight",
            slippage=0.0,
        )
        high_slippage_spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            signal_mode="weight",
            slippage=0.05,
        )

        zero_result = await run_backtest(zero_slippage_spec, prices, weights_df=weights_df)
        high_result = await run_backtest(high_slippage_spec, prices, weights_df=weights_df)

        assert zero_result.error is None
        assert high_result.error is None
        assert high_result.final_equity < zero_result.final_equity


class TestMetricsDelegateToComputeMetrics:
    @pytest.mark.asyncio
    async def test_sharpe_and_drawdown_match_compute_metrics_on_same_equity_curve(self):
        """run_backtest's sharpe/total_return/annual_return/max_drawdown must
        come from compute_metrics, not a parallel hand-rolled formula —
        recomputing compute_metrics on the returned equity curve should
        reproduce the result exactly."""
        prices = _make_prices(days=60)
        entries, exits = _make_binary_signals(prices)
        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
        )

        result = await run_backtest(spec, prices, entries=entries, exits=exits)
        assert result.error is None

        expected = compute_metrics(result.equity_curve, result.timestamps)
        assert result.sharpe_ratio == pytest.approx(expected.sharpe_ratio)
        assert result.total_return == pytest.approx(expected.total_return)
        assert result.annual_return == pytest.approx(expected.annual_return)
        assert result.max_drawdown == pytest.approx(expected.max_drawdown)
