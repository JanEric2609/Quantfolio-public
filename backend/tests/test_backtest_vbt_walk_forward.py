from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.lab.backtest_vbt.engine import BacktestResult, BacktestSpec
from app.lab.backtest_vbt.walk_forward import WalkForwardResult, walk_forward_test


def _make_spec(prices: pd.DataFrame, **overrides) -> BacktestSpec:
    return BacktestSpec(
        symbols=["AAPL"],
        start_date=prices.index[0].to_pydatetime(),  # type: ignore[index]
        end_date=prices.index[-1].to_pydatetime(),  # type: ignore[index]
        initial_cash=100000.0,
        weights={"AAPL": 1.0},
        rebalance_freq="D",
        commission=0.001,
        slippage=0.0005,
        seed=42,
        regime_context=None,
        regime_override=None,
        signal_mode="binary",
        **overrides,
    )


def _mock_backtest_result(initial_cash: float = 100000.0, total_return: float = 0.05) -> BacktestResult:
    final_equity = initial_cash * (1 + total_return)
    return BacktestResult(
        total_return=total_return,
        annual_return=total_return,
        sharpe_ratio=1.0,
        max_drawdown=-0.02,
        win_rate=0.6,
        trades=5,
        final_equity=final_equity,
        equity_curve=np.array([initial_cash, final_equity]),
        timestamps=np.array([datetime(2020, 1, 1), datetime(2020, 1, 2)]),
        regime_params={},
        error=None,
    )


def _rising_prices(length: int) -> pd.DataFrame:
    dates = pd.date_range(start="2020-01-01", periods=length, freq="D")
    prices = np.linspace(100.0, 200.0, length)
    return pd.DataFrame({"AAPL": prices}, index=dates)


@pytest.mark.asyncio
async def test_insufficient_data_returns_error_result():
    prices = _rising_prices(length=100)
    spec = _make_spec(prices)

    result = await walk_forward_test(
        spec, prices, train_period_days=252, test_period_days=63
    )

    assert isinstance(result, WalkForwardResult)
    assert result.periods_tested == 0
    assert result.out_sample_sharpe == 0.0
    assert result.in_sample_sharpe == 0.0
    assert result.avg_return_per_period == 0.0
    assert result.return_distribution == []
    assert result.errors == ["Insufficient data for walk-forward test"]


@pytest.mark.asyncio
async def test_sufficient_data_produces_sharpe_values():
    prices = _rising_prices(length=400)
    spec = _make_spec(prices)

    side_effect = [
        _mock_backtest_result(total_return=0.04),
        _mock_backtest_result(total_return=0.06),
        _mock_backtest_result(total_return=0.03),
        _mock_backtest_result(total_return=0.07),
    ]

    with patch(
        "app.lab.backtest_vbt.walk_forward.run_backtest",
        side_effect=side_effect,
    ):
        result = await walk_forward_test(
            spec, prices, train_period_days=252, test_period_days=63
        )

    assert isinstance(result, WalkForwardResult)
    assert result.periods_tested > 0
    assert result.errors == []
    assert np.isfinite(result.out_sample_sharpe)
    assert np.isfinite(result.in_sample_sharpe)
    assert len(result.return_distribution) == result.periods_tested
    assert result.avg_return_per_period == pytest.approx(0.065)


@pytest.mark.asyncio
async def test_walk_forward_with_backtest_spec_instance():
    prices = _rising_prices(length=400)
    spec = BacktestSpec(
        symbols=["AAPL"],
        start_date=prices.index[0].to_pydatetime(),  # type: ignore[index]
        end_date=prices.index[-1].to_pydatetime(),  # type: ignore[index]
        initial_cash=50000.0,
        weights={"AAPL": 1.0},
        rebalance_freq="D",
        commission=0.002,
        slippage=0.001,
        seed=123,
        regime_context=None,
        regime_override=None,
        signal_mode="binary",
    )

    side_effect = [
        _mock_backtest_result(initial_cash=50000.0, total_return=0.02),
        _mock_backtest_result(initial_cash=50000.0, total_return=0.04),
        _mock_backtest_result(initial_cash=50000.0, total_return=0.01),
        _mock_backtest_result(initial_cash=50000.0, total_return=0.05),
    ]

    with patch(
        "app.lab.backtest_vbt.walk_forward.run_backtest",
        side_effect=side_effect,
    ):
        result = await walk_forward_test(
            spec, prices, train_period_days=252, test_period_days=63
        )

    assert result.periods_tested > 0
    assert result.errors == []
    assert np.isfinite(result.out_sample_sharpe)
    assert np.isfinite(result.in_sample_sharpe)


@pytest.mark.asyncio
async def test_empty_prices_df_returns_error_result():
    empty_prices = pd.DataFrame()
    spec = BacktestSpec(
        symbols=["AAPL"],
        start_date=datetime(2020, 1, 1),
        end_date=datetime(2020, 1, 2),
        initial_cash=100000.0,
        weights={"AAPL": 1.0},
        rebalance_freq="D",
        commission=0.001,
        slippage=0.0005,
        seed=42,
        regime_context=None,
        regime_override=None,
        signal_mode="binary",
    )

    result = await walk_forward_test(
        spec, empty_prices, train_period_days=252, test_period_days=63
    )

    assert isinstance(result, WalkForwardResult)
    assert result.periods_tested == 0
    assert result.errors == ["Insufficient data for walk-forward test"]


@pytest.mark.asyncio
async def test_exact_minimum_rows_runs_one_period():
    train_days = 10
    test_days = 5
    prices = _rising_prices(length=train_days + test_days)
    spec = _make_spec(prices)

    with patch(
        "app.lab.backtest_vbt.walk_forward.run_backtest",
        return_value=_mock_backtest_result(total_return=0.01),
    ):
        result = await walk_forward_test(
            spec, prices, train_period_days=train_days, test_period_days=test_days
        )

    assert isinstance(result, WalkForwardResult)
    assert result.periods_tested == 1
    assert result.errors == []
    assert len(result.return_distribution) == 1
