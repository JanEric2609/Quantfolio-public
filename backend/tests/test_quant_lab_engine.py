"""Tests for the custom lab backtest engine (ADR 0015 ruling #25).

The vectorbt-oracle parity test is the concrete proof of "custom vectorised
engine; vectorbt as test oracle": an identical zero-cost, single-asset,
buy-and-hold-from-day-0 scenario is run through both engines and must agree
on final equity within a small numerical tolerance.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest
from app.lab.quant_lab.costs import CostModel
from app.lab.quant_lab.engine import LabBacktestSpec, run_lab_backtest


def _make_prices(days: int = 60, symbol: str = "AAPL") -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=days, freq="B")
    # Deterministic, monotone-ish walk (not random) so the expected final
    # equity is exactly computable, not just "close by construction."
    prices = 100.0 + np.cumsum(np.sin(np.arange(days) * 0.3) * 0.7 + 0.15)
    return pd.DataFrame({symbol: prices}, index=dates)


class TestVectorbtOracleParity:
    @pytest.mark.asyncio
    async def test_zero_cost_buy_and_hold_matches_vectorbt_final_equity(self):
        prices = _make_prices(days=60)
        symbol = "AAPL"
        initial_cash = 100_000.0

        # vectorbt oracle: enter on day 0 only, never exit -> buy-and-hold.
        n = len(prices)
        entries = pd.DataFrame({symbol: [i == 0 for i in range(n)]}, index=prices.index)
        exits = pd.DataFrame({symbol: [False] * n}, index=prices.index)
        vbt_spec = BacktestSpec(
            symbols=[symbol],
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 4, 1),
            initial_cash=initial_cash,
            commission=0.0,
            slippage=0.0,
        )
        vbt_result = await run_backtest(vbt_spec, prices, entries=entries, exits=exits)

        # quant_lab: a single rebalance event on day 0, weight=1.0, held thereafter.
        weights_df = pd.DataFrame(index=prices.index, columns=[symbol], dtype=float)
        weights_df.iloc[0] = 1.0
        lab_spec = LabBacktestSpec(
            symbols=[symbol],
            initial_cash=initial_cash,
            cost_model=CostModel(commission_bps=0.0, spread_bps=0.0, apply_tax_drag=False),
        )
        lab_result = run_lab_backtest(lab_spec, prices, weights_df)

        assert vbt_result.error is None
        assert lab_result.error is None
        assert lab_result.final_equity == pytest.approx(vbt_result.final_equity, rel=1e-6)
        assert lab_result.total_return == pytest.approx(vbt_result.total_return, rel=1e-6)

        # Sanity: both should equal the textbook buy-and-hold formula exactly.
        expected = initial_cash * (prices[symbol].iloc[-1] / prices[symbol].iloc[0])
        assert lab_result.final_equity == pytest.approx(expected, rel=1e-9)


class TestCostsReduceEquity:
    def test_costed_rebalance_produces_lower_equity_than_zero_cost(self):
        prices = _make_prices(days=60)
        symbol = "AAPL"
        weights_df = pd.DataFrame(index=prices.index, columns=[symbol], dtype=float)
        # Rebalance every 10 days to actually generate repeated trade costs.
        for i in range(0, len(prices), 10):
            weights_df.iloc[i] = 1.0 if (i // 10) % 2 == 0 else 0.5

        zero_cost_spec = LabBacktestSpec(
            symbols=[symbol], cost_model=CostModel(commission_bps=0.0, spread_bps=0.0, apply_tax_drag=False)
        )
        costed_spec = LabBacktestSpec(
            symbols=[symbol], cost_model=CostModel(commission_bps=50.0, spread_bps=50.0, apply_tax_drag=False)
        )

        zero_result = run_lab_backtest(zero_cost_spec, prices, weights_df)
        costed_result = run_lab_backtest(costed_spec, prices, weights_df)

        assert costed_result.trades == zero_result.trades
        assert costed_result.trades > 0
        assert costed_result.total_costs_eur > 0
        assert costed_result.final_equity < zero_result.final_equity


class TestTaxDragAppliedOnSells:
    def test_realized_gain_on_sell_incurs_tax_drag(self):
        # Monotone rising price so a sell always realizes a gain.
        dates = pd.date_range("2024-01-01", periods=10, freq="B")
        prices = pd.DataFrame({"AAPL": np.linspace(100.0, 150.0, 10)}, index=dates)
        weights_df = pd.DataFrame(index=dates, columns=["AAPL"], dtype=float)
        weights_df.iloc[0] = 1.0  # buy in full
        weights_df.iloc[5] = 0.0  # sell out entirely -> realizes a gain

        spec = LabBacktestSpec(
            symbols=["AAPL"],
            cost_model=CostModel(commission_bps=0.0, spread_bps=0.0, apply_tax_drag=True),
            teilfreistellung_pct={},
        )
        result = run_lab_backtest(spec, prices, weights_df)

        assert result.total_tax_drag_eur > 0
