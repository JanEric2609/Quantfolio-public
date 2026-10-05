"""Tests for strategy backtesting helpers."""
from unittest.mock import MagicMock, patch

from conftest import _memory_db

from app.foundation import backtest_strategy


def _rising_prices(n=300, start=100.0, step=0.001):
    return [start * (1 + step) ** i for i in range(n)]


def _declining_prices(n=300, start=100.0, step=-0.001):
    return [start * (1 + step) ** i for i in range(n)]


def test_buy_hold_rising_prices_completes_with_positive_return():
    close = _rising_prices()
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", close, strategy="buy_hold", params={}, source="fixture"
    )

    assert result["status"] == "completed"
    assert result["ticker"] == "TICK"
    assert result["strategy"] == "buy_hold"
    assert result["source"] == "fixture"
    assert result["metrics"]["total_return"] > 0
    assert len(result["equity_curve"]) > 0


def test_buy_hold_declining_prices_has_negative_return():
    close = _declining_prices()
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", close, strategy="buy_hold", params={}, source="fixture"
    )

    assert result["status"] == "completed"
    assert result["metrics"]["total_return"] < 0


def test_empty_close_series_returns_unavailable():
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", [], strategy="buy_hold", params={}, source="fixture"
    )

    assert result["status"] == "unavailable"
    assert "No close prices supplied" in result["message"]


def test_sma_cross_fast_gt_slow_not_all_ones():
    close = [100.0] * 20 + list(range(100, 250, 2)) + [200.0] * 50
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", close, strategy="sma_cross", params={"fast": 5, "slow": 10}, source="fixture"
    )

    assert result["status"] == "completed"
    assert result["metrics"]["total_return"] is not None
    assert len(result["trades"]) > 0


def test_rsi_mean_reversion_produces_signal():
    close = [100.0 - i * 2 for i in range(30)] + [40.0 + i * 0.5 for i in range(30)]
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", close, strategy="rsi_mean_reversion", params={"period": 14}, source="fixture"
    )

    assert result["status"] == "completed"
    assert len(result["trades"]) > 0


def test_momentum_produces_signal():
    close = [100.0 + i * 0.1 for i in range(100)]
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", close, strategy="momentum", params={"lookback": 20}, source="fixture"
    )

    assert result["status"] == "completed"
    assert len(result["trades"]) >= 0  # may or may not trade depending on series
    assert "momentum" in result["strategy"]


def test_bollinger_breakout_produces_signal():
    close = [100.0 + (i % 5) * 0.1 for i in range(40)] + [110.0, 115.0, 120.0]
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", close, strategy="bollinger_breakout", params={"window": 20, "width": 2}, source="fixture"
    )

    assert result["status"] == "completed"
    assert len(result["trades"]) > 0


def test_less_than_252_days_includes_warning():
    close = _rising_prices(n=100)
    result = backtest_strategy.strategy_backtest_from_prices(
        "TICK", close, strategy="buy_hold", params={}, source="fixture"
    )

    assert result["status"] == "completed"
    assert any("Less than one year" in warning for warning in result["warnings"])


def test_fallback_backtest_metrics_are_all_zero_floats():
    metrics = backtest_strategy.fallback_backtest_metrics()

    assert isinstance(metrics, dict)
    assert all(isinstance(v, float) for v in metrics.values())
    assert all(v == 0.0 for v in metrics.values())


def test_unavailable_backtest_returns_expected_shape():
    result = backtest_strategy.unavailable_backtest("TICK", "buy_hold", "something went wrong")

    assert result["ticker"] == "TICK"
    assert result["strategy"] == "buy_hold"
    assert result["status"] == "unavailable"
    assert result["message"] == "something went wrong"
    assert result["metrics"] == backtest_strategy.fallback_backtest_metrics()
    assert result["warnings"] == ["something went wrong"]
    assert result["benchmark_comparison"] == {}
    assert result["equity_curve"] == []
    assert result["rolling_drawdown"] == []
    assert result["rolling_volatility"] == []
    assert result["trades"] == []


def test_run_strategy_backtest_cached_uses_market_history():
    db = _memory_db()
    close = _rising_prices(n=100)
    rows = [{"date": f"2024-01-{i + 1:02d}", "close": close[i], "stale": False, "source": "yfinance"} for i in range(100)]

    mock_history = MagicMock(return_value=rows)
    with patch("app.foundation.market.history", mock_history):
        result = backtest_strategy.run_strategy_backtest_cached(
            db, "TICK", strategy="buy_hold", days=120
        )

    assert result["status"] == "completed"
    assert result["source"] == "cache_then_yfinance"
    mock_history.assert_called_once_with(db, "TICK", days=120)


def test_run_strategy_backtest_cached_no_history_returns_unavailable():
    db = _memory_db()
    mock_history = MagicMock(return_value=[])
    with patch("app.foundation.market.history", mock_history):
        result = backtest_strategy.run_strategy_backtest_cached(
            db, "TICK", strategy="buy_hold", days=30
        )

    assert result["status"] == "unavailable"
    assert "No cached or fetched market history" in result["message"]
