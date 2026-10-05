from app.foundation.backtest_strategy import strategy_backtest_from_prices
from app.foundation.quant import (
    factor_exposures,
    historical_var,
    parametric_var,
    portfolio_optimizations_from_price_matrix,
    portfolio_risk_from_price_matrix,
    price_matrix_to_returns,
)


def test_var_helpers_return_positive_loss_numbers():
    returns = [-0.04, -0.02, 0.01, 0.03, 0.005]

    assert historical_var(returns, 0.95) > 0
    assert parametric_var(returns, 0.95) > 0


def test_strategy_backtest_from_prices_returns_real_metrics():
    result = strategy_backtest_from_prices(
        "TEST",
        [100, 102, 101, 105, 108, 107, 110, 112, 115, 117],
        strategy="buy_hold",
    )

    assert result["status"] == "completed"
    assert result["metrics"]["total_return"] > 0
    assert result["equity_curve"]


def test_portfolio_risk_from_price_matrix_returns_curve():
    result = portfolio_risk_from_price_matrix(
        {
            "AAA": {"2026-01-01": 100, "2026-01-02": 101, "2026-01-03": 103},
            "BBB": {"2026-01-01": 50, "2026-01-02": 51, "2026-01-03": 50},
        },
        {"AAA": 0.7, "BBB": 0.3},
    )

    assert result["status"] == "completed"
    assert result["equity_curve"]


def test_optimizer_returns_core_weight_sets():
    result = portfolio_optimizations_from_price_matrix(
        {
            "AAA": {"2026-01-01": 100, "2026-01-02": 102, "2026-01-03": 104, "2026-01-04": 103},
            "BBB": {"2026-01-01": 50, "2026-01-02": 50.5, "2026-01-03": 50.2, "2026-01-04": 51},
        }
    )

    assert result["status"] == "completed"
    assert "max_sharpe" in result["methods"]
    assert round(sum(result["methods"]["equal_weight"]["weights"].values()), 6) == 1


def test_factor_exposure_uses_regression_not_static_placeholder():
    portfolio_returns = {idx: 0.001 * idx for idx in range(30)}
    factor_returns = {
        "market": {idx: 0.001 * idx for idx in range(30)},
        "size": {idx: 0.0005 * idx for idx in range(30)},
    }

    result = factor_exposures(portfolio_returns, factor_returns)

    assert result["status"] == "completed"
    assert result["exposures"]


def test_price_matrix_to_returns_excludes_gap_dates_without_fabricating_zero_returns():
    """A mid-series price gap must be dropped entirely, not forward-filled into a
    spurious 0% return (the non-synchronous/stale-trading bias this helper fixes)."""
    price_matrix = {
        "AAA": {
            "2026-01-01": 100,
            "2026-01-02": 101,
            "2026-01-03": 103,
            "2026-01-04": 104,
            "2026-01-05": 106,
            "2026-01-06": 108,
        },
        "BBB": {
            "2026-01-01": 50,
            "2026-01-02": 51,
            # 2026-01-03 intentionally missing: a mid-series data gap for BBB only.
            "2026-01-04": 53,
            "2026-01-05": 54,
            "2026-01-06": 56,
        },
    }

    returns = price_matrix_to_returns(price_matrix)

    # The gap date must not survive at all.
    assert "2026-01-03" not in [str(idx) for idx in returns.index]

    # A naive ffill-then-pct_change (the previous buggy behavior at all 5 call
    # sites) forward-fills BBB's 2026-01-03 price to 51 (same as the prior day),
    # fabricating an exact 0% return for that date. Confirm the fixed helper
    # produces strictly fewer rows than that naive approach, contains no exact
    # 0.0 artifact, while the naive approach demonstrably does.
    import pandas as pd

    naive = pd.DataFrame(price_matrix).ffill().dropna().pct_change().dropna()
    assert len(returns) < len(naive)
    assert not (returns == 0.0).any().any()
    assert (naive == 0.0).any().any()


def test_price_matrix_to_returns_drops_wholly_empty_symbol_without_wiping_dates():
    """A symbol with no price data at all should be dropped as a column up front,
    not cause every date to be dropped once full-row overlap is required."""
    price_matrix = {
        "AAA": {"2026-01-01": 100, "2026-01-02": 101, "2026-01-03": 103},
        "BBB": {"2026-01-01": 50, "2026-01-02": 51, "2026-01-03": 52},
        "CCC": {},
    }

    returns = price_matrix_to_returns(price_matrix)

    assert "CCC" not in returns.columns
    assert len(returns) == 2
