"""Tests for signal-driven VBT backtester (Phase 2)."""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest


def _make_prices(symbols: list[str], days: int = 60) -> pd.DataFrame:
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=days, freq="B")
    data = {}
    for s in symbols:
        prices = 100 + np.cumsum(np.random.randn(days) * 0.5)
        data[s] = prices
    return pd.DataFrame(data, index=dates)


def _make_binary_signals(
    prices_df: pd.DataFrame, entry_pct: float = 0.5
) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = len(prices_df)
    entries = pd.DataFrame(
        {col: [True if i == 0 else False for i in range(n)] for col in prices_df.columns},
        index=prices_df.index,
    )
    exits = pd.DataFrame(
        {col: [False] * n for col in prices_df.columns},
        index=prices_df.index,
    )
    return entries, exits


def _make_weight_signals(
    prices_df: pd.DataFrame, weight: float = 0.5
) -> pd.DataFrame:
    return pd.DataFrame(
        {col: [weight] * len(prices_df) for col in prices_df.columns},
        index=prices_df.index,
    )


class TestBacktestSpecSignalMode:
    def test_default_signal_mode_is_binary(self):
        from app.lab.backtest_vbt.engine import BacktestSpec

        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 12, 31),
        )
        assert spec.signal_mode == "binary"

    def test_accepts_weight_mode(self):
        from app.lab.backtest_vbt.engine import BacktestSpec

        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 12, 31),
            signal_mode="weight",
        )
        assert spec.signal_mode == "weight"


class TestSignalBacktestBinary:
    @pytest.mark.asyncio
    async def test_always_long_fallback(self):
        from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest

        prices = _make_prices(["AAPL"], days=30)
        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 2, 15),
        )
        result = await run_backtest(spec, prices)
        assert result.error is None
        assert result.trades >= 0
        assert result.final_equity > 0

    @pytest.mark.asyncio
    async def test_binary_entry_signal(self):
        from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest

        prices = _make_prices(["AAPL"], days=60)
        entries, exits = _make_binary_signals(prices)
        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            signal_mode="binary",
        )
        result = await run_backtest(spec, prices, entries=entries, exits=exits)
        assert result.error is None
        assert result.trades >= 1
        assert result.final_equity > 0

    @pytest.mark.asyncio
    async def test_binary_multiple_symbols(self):
        from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest

        prices = _make_prices(["AAPL", "MSFT", "GOOGL"], days=60)
        entries, exits = _make_binary_signals(prices)
        spec = BacktestSpec(
            symbols=["AAPL", "MSFT", "GOOGL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            signal_mode="binary",
        )
        result = await run_backtest(spec, prices, entries=entries, exits=exits)
        assert result.error is None
        assert result.equity_curve.shape[0] > 0


class TestSignalBacktestWeight:
    @pytest.mark.asyncio
    async def test_weight_mode_basic(self):
        from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest

        prices = _make_prices(["AAPL"], days=60)
        weights = _make_weight_signals(prices, weight=0.5)
        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            signal_mode="weight",
        )
        result = await run_backtest(spec, prices, weights_df=weights)
        assert result.error is None
        assert result.trades >= 1
        assert result.final_equity > 0

    @pytest.mark.asyncio
    async def test_weight_mode_zero_weight(self):
        from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest

        prices = _make_prices(["AAPL"], days=60)
        weights = pd.DataFrame(
            {"AAPL": [0.0] * 60},
            index=prices.index,
        )
        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            signal_mode="weight",
        )
        result = await run_backtest(spec, prices, weights_df=weights)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_weight_mode_multi_symbol(self):
        from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest

        prices = _make_prices(["AAPL", "MSFT"], days=60)
        weights = _make_weight_signals(prices, weight=0.5)
        spec = BacktestSpec(
            symbols=["AAPL", "MSFT"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            signal_mode="weight",
        )
        result = await run_backtest(spec, prices, weights_df=weights)
        assert result.error is None
        assert result.equity_curve.shape[0] > 0


class TestSignalBacktestEmpty:
    @pytest.mark.asyncio
    async def test_empty_prices(self):
        from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest

        prices = pd.DataFrame()
        spec = BacktestSpec(
            symbols=["AAPL"],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 12, 31),
        )
        result = await run_backtest(spec, prices)
        assert result.error is not None
        assert result.trades == 0
