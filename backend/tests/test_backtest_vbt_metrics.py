"""Tests for backtest_vbt metrics computation."""

import numpy as np
import pandas as pd
import pytest

from app.foundation import quant_metrics
from app.lab.backtest_vbt.metrics import BacktestMetrics, compute_metrics


# ---------------------------------------------------------------------------
# Drawdown-duration regression suite (locked semantics).
#
# ``BacktestMetrics.drawdown_duration_days`` MUST be the LONGEST CONSECUTIVE
# UNDERWATER STREAK (peak-to-peak time between new equity highs), NOT the count
# of drawdown episodes. A prior gemini commit (a1bbc87) broke this by computing
# the number of rising edges of the in-drawdown mask (== episode count). The
# fixed code delegates to ``quant_metrics.max_drawdown(...)["max_drawdown_duration"]``.
#
# These tests FAIL on the buggy code and PASS on the fixed code -- with one
# documented edge case (flat curve) where the task spec and the current source
# disagree; see ``test_drawdown_duration_flat_curve``.
# ---------------------------------------------------------------------------


def _regression_metrics(curve):
    """compute_metrics over a synthetic per-step timestamp axis (no DB/mocks)."""
    equity = np.array(curve, dtype=float)
    return compute_metrics(
        equity_curve=equity,
        timestamps=np.arange(len(equity)),
        trades=None,
        periods_per_year=252,
    )


def _date_range(days: int) -> np.ndarray:
    return pd.date_range("2024-01-01", periods=days, freq="D").to_numpy()


def test_dataclass_can_be_instantiated():
    metrics = BacktestMetrics(
        sharpe_ratio=1.0,
        sortino_ratio=1.2,
        calmar_ratio=0.8,
        max_drawdown=-0.1,
        drawdown_duration_days=5,
        total_return=0.2,
        annual_return=0.25,
        win_rate=0.55,
        profit_factor=1.5,
        return_std=0.02,
    )

    assert metrics.sharpe_ratio == 1.0
    assert metrics.sortino_ratio == 1.2


def test_rising_equity_curve_produces_positive_metrics():
    # Curve ends higher but includes a small dip so Calmar is defined.
    equity = np.array([100.0, 102.0, 99.0, 103.0, 105.0])
    timestamps = _date_range(len(equity))

    metrics = compute_metrics(equity, timestamps)

    assert metrics.total_return > 0
    assert metrics.sharpe_ratio > 0
    assert metrics.sortino_ratio > 0
    assert metrics.calmar_ratio > 0
    assert metrics.max_drawdown < 0
    assert metrics.drawdown_duration_days == 1


def test_flat_equity_curve_produces_zero_metrics():
    equity = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
    timestamps = _date_range(len(equity))

    metrics = compute_metrics(equity, timestamps)

    assert metrics.total_return == 0.0
    assert metrics.annual_return == 0.0
    assert metrics.sharpe_ratio == 0.0
    assert metrics.sortino_ratio == 0.0
    assert metrics.calmar_ratio == 0.0
    assert metrics.max_drawdown == 0.0
    assert metrics.win_rate == 0.0
    assert metrics.return_std == 0.0


def test_declining_equity_produces_negative_returns_and_drawdown():
    equity = np.array([100.0, 99.0, 98.0, 97.0, 96.0])
    timestamps = _date_range(len(equity))

    metrics = compute_metrics(equity, timestamps)

    assert metrics.total_return < 0
    assert metrics.annual_return < 0
    assert metrics.max_drawdown < 0


def test_drawdown_duration_counts_dip_episodes():
    # Peak -> dip below -0.1% -> recovery produces one drawdown episode.
    equity = np.array([100.0, 110.0, 95.0, 111.0, 115.0])
    timestamps = _date_range(len(equity))

    metrics = compute_metrics(equity, timestamps)

    assert metrics.max_drawdown < -0.001
    assert metrics.drawdown_duration_days == 1


def test_win_rate_computed_from_trades():
    equity = np.array([100.0, 100.0, 100.0, 100.0])
    timestamps = _date_range(len(equity))
    trades = [
        {"pnl": 100.0},
        {"pnl": -50.0},
        {"pnl": 25.0},
        {"pnl": 0.0},
    ]

    metrics = compute_metrics(equity, timestamps, trades=trades)

    # 2 winning trades out of 4 total.
    assert metrics.win_rate == 0.5


def test_profit_factor_computed_from_trades():
    equity = np.array([100.0, 100.0, 100.0, 100.0])
    timestamps = _date_range(len(equity))
    trades = [
        {"pnl": 100.0},
        {"pnl": 50.0},
        {"pnl": -30.0},
        {"pnl": -20.0},
    ]

    metrics = compute_metrics(equity, timestamps, trades=trades)

    # Total profit = 150, total loss = 50 -> profit factor = 3.0.
    assert metrics.profit_factor == 3.0


def test_profit_factor_is_one_when_no_losses():
    equity = np.array([100.0, 100.0])
    timestamps = _date_range(len(equity))
    trades = [{"pnl": 50.0}, {"pnl": 25.0}]

    metrics = compute_metrics(equity, timestamps, trades=trades)

    assert metrics.profit_factor == 1.0


def test_win_rate_computed_from_returns_when_no_trades():
    equity = np.array([100.0, 101.0, 99.0, 102.0])
    timestamps = _date_range(len(equity))

    metrics = compute_metrics(equity, timestamps)

    returns = np.diff(equity) / equity[:-1]
    expected_win_rate = np.sum(returns > 0) / len(returns)
    assert metrics.win_rate == expected_win_rate


def test_single_element_equity_curve_handled():
    equity = np.array([100.0])
    timestamps = _date_range(len(equity))

    metrics = compute_metrics(equity, timestamps)

    assert metrics.total_return == 0.0
    assert metrics.annual_return == 0.0
    assert metrics.return_std == 0.0
    assert metrics.max_drawdown == 0.0


def test_empty_equity_curve_returns_zero_metrics():
    equity = np.array([])
    timestamps = np.array([])

    metrics = compute_metrics(equity, timestamps)

    assert metrics.total_return == 0.0
    assert metrics.annual_return == 0.0
    assert metrics.return_std == 0.0
    assert metrics.max_drawdown == 0.0


def test_drawdown_duration_is_longest_streak_not_episode_count():
    """Two drawdown episodes but the LONGEST streak is 5, not 2 episodes.

    Curve: 100 -> 110 (peak) -> 100 (5-step decline, streak 5)
           -> 111 (new high) -> 105 (3-step decline, streak 3).
    Old buggy code returned 2 (episode count); correct answer is 5.
    """
    curve = [100, 110, 108, 106, 104, 102, 100, 111, 109, 107, 105, 112]
    result = _regression_metrics(curve)

    assert result.drawdown_duration_days == 5
    # Deepest single-episode drawdown: 100/110 - 1.
    assert result.max_drawdown == pytest.approx(-0.090909, abs=1e-4)


def test_drawdown_duration_single_long_episode():
    """One peak then a single 10-step decline -> longest streak is 10.

    Old buggy code returned 1 (it counted only one episode); correct is 10.
    """
    curve = [100, 120] + [120 - i for i in range(1, 11)]  # peak 120, then 119..110
    result = _regression_metrics(curve)

    assert result.drawdown_duration_days == 10


def test_drawdown_duration_flat_curve():
    """A perfectly flat equity curve has zero drawdown depth.

    NOTE (edge case / finding): the task spec asserts ``duration == 0`` here,
    but the CURRENT ``quant_metrics.max_drawdown`` returns ``duration == 3``
    (len-1) for a flat curve, because it resets the underwater streak only on
    ``equity > peak`` (strict), so periods sitting exactly AT the running peak
    are still counted as underwater. This test documents that discrepancy:
    it asserts the spec's expected ``max_drawdown == 0.0`` (which passes) and
    the spec's expected ``duration == 0`` (which FAILS against current source).
    Left as-is so maintainers can decide whether to treat at-peak as
    underwater. Do NOT edit source to mask this; report it instead.
    """
    curve = [100, 100, 100, 100]
    result = _regression_metrics(curve)

    # Depth is unambiguously zero (no decline at all).
    assert result.max_drawdown == 0.0
    # Spec expectation; fails against current source (returns 3) -- see docstring.
    assert result.drawdown_duration_days == 0


def test_drawdown_duration_open_at_end():
    """Curve ends underwater (never recovers) -> streak is 1, not 0."""
    curve = [100, 110, 105]  # peak 110, ends at 105
    result = _regression_metrics(curve)

    assert result.drawdown_duration_days == 1


def test_canonical_max_drawdown_duration_contract():
    """compute_metrics must rely on the single-source quant_metrics contract.

    Using the SAME returns derived from curve #1, call
    ``quant_metrics.max_drawdown`` directly and confirm it reports the same
    longest-streak duration (5) and deepest drawdown (~ -0.0909). This pins the
    delegation contract so a future regression in either function is caught.
    """
    curve = [100, 110, 108, 106, 104, 102, 100, 111, 109, 107, 105, 112]
    equity = np.array(curve, dtype=float)
    returns = np.diff(equity) / equity[:-1]

    dd = quant_metrics.max_drawdown(returns.tolist())

    assert dd["max_drawdown_duration"] == 5
    assert dd["max_drawdown"] == pytest.approx(-0.090909, abs=1e-4)
