"""Walk-forward cross-validation for AlphaCrafter Trader (Phase 4)."""

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest


class SignalGenerator(Protocol):
    """A fittable strategy: fit params on train, generate signals on any window."""

    def fit(self, train_df: pd.DataFrame): ...

    def generate(
        self, prices_df: pd.DataFrame, params
    ) -> tuple[pd.DataFrame, pd.DataFrame]: ...


@dataclass
class WalkForwardResult:
    """Result of a walk-forward test."""

    out_sample_sharpe: float
    in_sample_sharpe: float
    periods_tested: int
    avg_return_per_period: float
    return_distribution: list[float]
    errors: list[str]
    # False means no per-window strategy signals were applied, so the numbers
    # below describe the static weighted (buy-and-hold) allocation on each
    # window — NOT a fitted strategy. See docstring / proposals doc.
    strategy_applied: bool = False


async def walk_forward_test(
    spec: BacktestSpec,
    prices_df: pd.DataFrame,
    train_period_days: int = 252,
    test_period_days: int = 63,
    signal_generator: SignalGenerator | None = None,
) -> WalkForwardResult:
    """Run walk-forward cross-validation on a backtest specification.

    When *signal_generator* is provided, this is a genuine strategy
    walk-forward: on each window the generator is **fit on the training data**,
    the fitted parameters are applied to generate entry/exit signals on **both**
    the train and the test window, and those signals drive the backtest. The
    out-of-sample figures then measure the fitted strategy's skill on unseen
    data, and ``strategy_applied`` is set True.

    When *signal_generator* is None (the default), no strategy is fit and
    ``run_backtest`` falls back to a static weighted buy-and-hold on every
    window — this measures the stability of the *allocation* across time, not
    out-of-sample strategy skill, so ``strategy_applied`` is left False.

    Args:
        spec: Backtest specification
        prices_df: Price history with datetime index
        train_period_days: Training window size (default 1 year)
        test_period_days: Testing window size (default 1 quarter)
        signal_generator: Optional fittable strategy applied per window.

    Returns:
        WalkForwardResult with out-sample Sharpe, in-sample Sharpe, and distribution
    """
    if prices_df.empty or len(prices_df) < train_period_days + test_period_days:
        return WalkForwardResult(
            out_sample_sharpe=0.0,
            in_sample_sharpe=0.0,
            periods_tested=0,
            avg_return_per_period=0.0,
            return_distribution=[],
            errors=["Insufficient data for walk-forward test"],
        )

    out_sample_returns = []
    in_sample_returns = []
    errors = []
    periods = 0

    # Start with initial training period
    current_idx = 0
    while current_idx + train_period_days + test_period_days <= len(prices_df):
        # Split data
        train_data = prices_df.iloc[current_idx : current_idx + train_period_days]
        test_data = prices_df.iloc[
            current_idx + train_period_days : current_idx + train_period_days + test_period_days
        ]

        # Run in-sample backtest on training data
        train_spec = BacktestSpec(
            symbols=spec.symbols,
            start_date=train_data.index[0],
            end_date=train_data.index[-1],
            initial_cash=spec.initial_cash,
            weights=spec.weights,
            commission=spec.commission,
        )
        # Run out-of-sample backtest on test data
        test_spec = BacktestSpec(
            symbols=spec.symbols,
            start_date=test_data.index[0],
            end_date=test_data.index[-1],
            initial_cash=spec.initial_cash,
            weights=spec.weights,
            commission=spec.commission,
        )

        if signal_generator is not None:
            # Fit on the training window only, then apply the fitted signals to
            # both windows so in- and out-of-sample both reflect the strategy.
            params = signal_generator.fit(train_data)
            train_entries, train_exits = signal_generator.generate(train_data, params)
            # Warm-up fix: generate() applied to test_data in isolation has no
            # trailing history, so a fitted rolling window (e.g. SMA slow >=
            # test_period_days, plausible with the default slow_grid maxing
            # at 200 vs test_period_days=63) never leaves its NaN warm-up
            # period — the OOS leg goes silently flat regardless of what the
            # fitted strategy would actually signal. Prepending train_data as
            # trailing context (then slicing back to test_data's own index)
            # gives every rolling computation up to a full training window of
            # warm-up, and also fixes the position-shift at the train/test
            # boundary (prev-day position must come from the real prior day,
            # not an artificial False).
            extended_df = pd.concat([train_data, test_data])
            extended_entries, extended_exits = signal_generator.generate(extended_df, params)
            test_entries = extended_entries.loc[test_data.index]
            test_exits = extended_exits.loc[test_data.index]
            train_result = await run_backtest(
                train_spec, train_data, train_entries, train_exits
            )
            test_result = await run_backtest(
                test_spec, test_data, test_entries, test_exits
            )
        else:
            train_result = await run_backtest(train_spec, train_data)
            test_result = await run_backtest(test_spec, test_data)

        if train_result.error is None:
            in_sample_returns.append(train_result.total_return)
        else:
            errors.append(f"Train period {periods}: {train_result.error}")

        if test_result.error is None:
            out_sample_returns.append(test_result.total_return)
        else:
            errors.append(f"Test period {periods}: {test_result.error}")

        current_idx += test_period_days
        periods += 1

    # Compute aggregate statistics.
    # Each element of out_sample_returns / in_sample_returns is a total return
    # for one test window of length `test_period_days` (default 63 trading days).
    # Treating those as periodic returns, the annualisation factor is
    # sqrt(252 / test_period_days) — i.e. how many non-overlapping test windows
    # fit in a trading year.
    # Each return is a total return over its own window, so it must be annualised
    # with the factor for THAT window length. In-sample windows are
    # train_period_days long; out-of-sample windows are test_period_days long.
    # Using the test factor for both (the previous behaviour) inflated the
    # in-sample Sharpe by sqrt(train/test) ≈ 2x at the defaults.
    out_annualisation = np.sqrt(252 / test_period_days)
    in_annualisation = np.sqrt(252 / train_period_days)
    out_sample_sharpe = (
        (np.mean(out_sample_returns) / np.std(out_sample_returns, ddof=1)) * out_annualisation
        if len(out_sample_returns) > 1
        else 0.0
    )
    in_sample_sharpe = (
        (np.mean(in_sample_returns) / np.std(in_sample_returns, ddof=1)) * in_annualisation
        if len(in_sample_returns) > 1
        else 0.0
    )
    avg_return = np.mean(out_sample_returns) if out_sample_returns else 0.0

    return WalkForwardResult(
        out_sample_sharpe=float(out_sample_sharpe),
        in_sample_sharpe=float(in_sample_sharpe),
        periods_tested=periods,
        avg_return_per_period=float(avg_return),
        return_distribution=out_sample_returns,
        errors=errors,
        strategy_applied=signal_generator is not None,
    )
