"""Tests for real strategy walk-forward (proposal P3).

Previously the walk-forward validator's out-of-sample leg was buy-and-hold: the
fitted strategy was never applied to the test window. These tests cover the
fittable signal generator and that walk_forward_test, when given one, fits on
train and applies the fitted signals on test (strategy_applied=True).
"""
from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.lab.backtest_vbt.engine import BacktestResult, BacktestSpec
from app.lab.backtest_vbt.signals import (
    FittedParams,
    SmaCrossoverSignalGenerator,
)
from app.lab.backtest_vbt.walk_forward import walk_forward_test


def _series(values) -> pd.DataFrame:
    dates = pd.date_range(start="2020-01-01", periods=len(values), freq="D")
    return pd.DataFrame({"AAPL": np.asarray(values, dtype=float)}, index=dates)


def _rising(length: int) -> pd.DataFrame:
    return _series(np.linspace(100.0, 200.0, length))


def _mock_result(total_return: float = 0.03) -> BacktestResult:
    fe = 100000.0 * (1 + total_return)
    return BacktestResult(
        total_return=total_return, annual_return=total_return, sharpe_ratio=1.0,
        max_drawdown=-0.02, win_rate=0.6, trades=5, final_equity=fe,
        equity_curve=np.array([100000.0, fe]),
        timestamps=np.array([datetime(2020, 1, 1), datetime(2020, 1, 2)]),
        regime_params={}, error=None,
    )


def test_fit_returns_fast_slower_than_slow():
    gen = SmaCrossoverSignalGenerator(fast_grid=(5, 10), slow_grid=(20, 40))
    params = gen.fit(_rising(200))
    assert isinstance(params, FittedParams)
    assert params.fast < params.slow


def test_generate_produces_boolean_entries_and_exits_aligned_to_input():
    # Rise then fall so a crossover strategy both enters and exits.
    up = np.linspace(100.0, 160.0, 120)
    down = np.linspace(160.0, 90.0, 120)
    prices = _series(np.concatenate([up, down]))
    gen = SmaCrossoverSignalGenerator()
    entries, exits = gen.generate(prices, FittedParams(fast=10, slow=30))

    assert list(entries.columns) == list(prices.columns)
    assert list(entries.index) == list(prices.index)
    assert entries.dtypes["AAPL"] == bool
    assert exits.dtypes["AAPL"] == bool
    assert entries["AAPL"].any()  # entered on the up-cross
    assert exits["AAPL"].any()    # exited on the down-cross


@pytest.mark.asyncio
async def test_walk_forward_with_generator_applies_signals_and_flags_it():
    prices = _rising(400)
    spec = BacktestSpec(
        symbols=["AAPL"], start_date=prices.index[0].to_pydatetime(),
        end_date=prices.index[-1].to_pydatetime(), initial_cash=100000.0,
        weights={"AAPL": 1.0},
    )
    calls = []

    async def _capture(spec_arg, df_arg, entries=None, exits=None, weights_df=None):
        calls.append({"entries": entries, "exits": exits})
        return _mock_result(0.03)

    with patch("app.lab.backtest_vbt.walk_forward.run_backtest", side_effect=_capture):
        result = await walk_forward_test(
            spec, prices, train_period_days=252, test_period_days=63,
            signal_generator=SmaCrossoverSignalGenerator(),
        )

    assert result.strategy_applied is True
    assert result.periods_tested > 0
    # Every backtest call received applied entry/exit signals (not buy-and-hold).
    assert calls, "run_backtest was never called"
    assert all(c["entries"] is not None and c["exits"] is not None for c in calls)


@pytest.mark.asyncio
async def test_walk_forward_without_generator_stays_buy_and_hold():
    prices = _rising(400)
    spec = BacktestSpec(
        symbols=["AAPL"], start_date=prices.index[0].to_pydatetime(),
        end_date=prices.index[-1].to_pydatetime(), initial_cash=100000.0,
        weights={"AAPL": 1.0},
    )
    calls = []

    async def _capture(spec_arg, df_arg, entries=None, exits=None, weights_df=None):
        calls.append({"entries": entries, "exits": exits})
        return _mock_result(0.03)

    with patch("app.lab.backtest_vbt.walk_forward.run_backtest", side_effect=_capture):
        result = await walk_forward_test(
            spec, prices, train_period_days=252, test_period_days=63,
        )

    assert result.strategy_applied is False
    assert all(c["entries"] is None and c["exits"] is None for c in calls)


# ---------------------------------------------------------------------------
# Track C1 — warm-up bug: generate() applied to an isolated window has no
# trailing history, so a fitted rolling window >= the window length never
# leaves its NaN warm-up period. A monotonically rising series makes this
# deterministic to test: once warmed up, fast SMA > slow SMA holds for every
# row, so the position should be True throughout the test window ONLY when
# real trailing history informs it.
# ---------------------------------------------------------------------------

def test_position_is_flat_when_generate_called_on_isolated_window_without_warmup():
    """Documents the bug directly: SmaCrossoverSignalGenerator.generate(),
    called on a window shorter than `slow`, produces an all-flat position —
    this is exactly why walk_forward_test must never call generate() on
    test_data alone when slow can exceed test_period_days."""
    gen = SmaCrossoverSignalGenerator()
    test = _rising(63)  # shorter than slow=100
    params = FittedParams(fast=5, slow=100)

    pos = gen._position(test.iloc[:, 0], params.fast, params.slow)

    assert not pos.any()


def test_position_warms_up_correctly_with_trailing_history():
    """The fix: prepending trailing history (train_data) before generate()
    gives the rolling window enough history to warm up within the test
    window, even when slow >= test_period_days."""
    gen = SmaCrossoverSignalGenerator()
    train = _rising(252)
    # Continue rising from train's last price (not reset to 100) — a drop at
    # the train/test boundary would itself trigger a real crossover, which
    # would confound this test's premise (fast SMA stays above slow SMA
    # throughout because the series never stops rising).
    train_last_price = float(train.iloc[-1, 0])
    test_values = np.linspace(train_last_price, train_last_price + 50.0, 63)
    test = pd.DataFrame(
        {"AAPL": test_values},
        index=pd.date_range(start=train.index[-1] + pd.Timedelta(days=1), periods=63, freq="D"),
    )
    params = FittedParams(fast=5, slow=100)

    extended = pd.concat([train, test])
    extended_pos = gen._position(extended.iloc[:, 0], params.fast, params.slow)
    sliced_pos = extended_pos.loc[test.index]

    # Monotonically rising throughout train+test: once warmed up, fast SMA
    # stays above slow SMA for the entire test window.
    assert sliced_pos.all()


@pytest.mark.asyncio
async def test_walk_forward_passes_trailing_history_into_generate_for_test_window():
    """Integration-level guard for the walk_forward.py wiring fix: the
    DataFrame passed to generate() for the out-of-sample leg must carry
    train_data as trailing context, not just the isolated test window —
    otherwise a fitted slow window >= test_period_days can never warm up."""
    prices = _rising(400)
    spec = BacktestSpec(
        symbols=["AAPL"], start_date=prices.index[0].to_pydatetime(),
        end_date=prices.index[-1].to_pydatetime(), initial_cash=100000.0,
        weights={"AAPL": 1.0},
    )

    async def _fake_run_backtest(spec_arg, df_arg, entries=None, exits=None, weights_df=None):
        return _mock_result(0.03)

    generate_calls: list[pd.DataFrame] = []
    gen = SmaCrossoverSignalGenerator(fast_grid=(5,), slow_grid=(100,))
    original_generate = gen.generate

    def _tracking_generate(prices_df, params):
        generate_calls.append(prices_df)
        return original_generate(prices_df, params)

    gen.generate = _tracking_generate

    with patch("app.lab.backtest_vbt.walk_forward.run_backtest", side_effect=_fake_run_backtest):
        await walk_forward_test(
            spec, prices, train_period_days=252, test_period_days=63,
            signal_generator=gen,
        )

    # Each period makes 2 generate() calls: one for train_data (252 rows),
    # one for the out-of-sample leg. Before the fix, the second call always
    # received exactly the 63-row isolated test_data; after the fix it
    # receives train_data + test_data concatenated.
    assert generate_calls
    test_leg_calls = generate_calls[1::2]
    assert test_leg_calls
    assert all(len(df) > 63 for df in test_leg_calls)
