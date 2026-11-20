"""Edge case tests for critical functions across quant_metrics, quant_factors, and quant_optim."""
from __future__ import annotations

import math

import numpy as np

from app.foundation import quant_metrics


class TestQuantMetricsEdgeCases:
    def test_sharpe_empty_returns(self) -> None:
        assert quant_metrics.sharpe_ratio([]) == 0.0

    def test_sharpe_single_return(self) -> None:
        assert quant_metrics.sharpe_ratio([0.01]) == 0.0

    def test_sortino_empty_returns(self) -> None:
        assert quant_metrics.sortino_ratio([]) == 0.0

    def test_sortino_single_return(self) -> None:
        assert quant_metrics.sortino_ratio([0.01]) == 0.0

    def test_max_drawdown_empty(self) -> None:
        result = quant_metrics.max_drawdown([])
        assert result["max_drawdown"] == 0.0
        assert result["max_drawdown_duration"] == 0.0

    def test_max_drawdown_single_return(self) -> None:
        result = quant_metrics.max_drawdown([0.01])
        assert result["max_drawdown"] == 0.0

    def test_calmar_empty_returns(self) -> None:
        assert quant_metrics.calmar_ratio([]) == 0.0

    def test_historical_cvar_empty(self) -> None:
        assert quant_metrics.historical_cvar([]) == 0.0

    def test_parametric_cvar_empty(self) -> None:
        assert quant_metrics.parametric_cvar([]) == 0.0

    def test_beta_empty_portfolios(self) -> None:
        assert quant_metrics.beta([], [0.01, 0.02]) == 0.0

    def test_beta_empty_benchmarks(self) -> None:
        assert quant_metrics.beta([0.01, 0.02], []) == 0.0

    def test_r_squared_empty(self) -> None:
        assert quant_metrics.r_squared([], []) == 0.0

    def test_skewness_single_observation(self) -> None:
        assert quant_metrics.skewness([0.01]) == 0.0

    def test_kurtosis_single_observation(self) -> None:
        assert quant_metrics.kurtosis([0.01]) == 0.0

    def test_kelly_fraction_empty(self) -> None:
        assert quant_metrics.kelly_fraction([]) == 0.0

    def test_volatility_drag_empty(self) -> None:
        assert quant_metrics.volatility_drag([]) == 0.0

    def test_rolling_sharpe_empty(self) -> None:
        assert quant_metrics.rolling_sharpe([], window=20) == []

    def test_annualised_return_all_zeros(self) -> None:
        assert quant_metrics.annualised_return([0.0] * 100) == 0.0

    def test_annualised_volatility_all_zeros(self) -> None:
        assert quant_metrics.annualised_volatility([0.0] * 100) == 0.0

    def test_extreme_positive_returns(self) -> None:
        returns = [1.0] * 10  # 100% daily returns
        ann = quant_metrics.annualised_return(returns)
        assert ann > 0
        assert math.isfinite(ann)

    def test_extreme_negative_returns(self) -> None:
        returns = [-0.5] * 10  # -50% daily returns
        ann = quant_metrics.annualised_return(returns)
        assert ann < 0

    def test_mixed_extreme_values(self) -> None:
        returns = [0.5, -0.5, 0.5, -0.5, 0.5]
        sharpe = quant_metrics.sharpe_ratio(returns)
        assert math.isfinite(sharpe)

    def test_full_risk_report_empty(self) -> None:
        report = quant_metrics.full_risk_report([])
        assert report["samples"] == 0

    def test_full_risk_report_without_benchmark(self) -> None:
        returns = [0.01, -0.02, 0.015]
        report = quant_metrics.full_risk_report(returns)
        assert "beta" not in report
        assert "alpha" not in report


class TestQuantFactorsEdgeCases:
    def test_newey_west_empty_residuals(self) -> None:
        from app.foundation.quant_factors import newey_west_se

        result = newey_west_se([], np.ones((0, 2)))
        assert result["hac_cov"] is None

    def test_newey_west_single_observation(self) -> None:
        from app.foundation.quant_factors import newey_west_se

        result = newey_west_se([0.01], [[1.0, 0.5]])
        assert result["n"] == 1
        assert result["hac_cov"] is None

    def test_compute_factor_definitions_returns_list(self) -> None:
        from app.foundation.quant_factors import get_factor_definitions

        defs = get_factor_definitions()
        assert isinstance(defs, list)
        assert len(defs) > 0

    def test_list_factor_zoo_returns_list(self) -> None:
        from app.foundation.quant_factors import list_factor_zoo

        zoo = list_factor_zoo()
        assert isinstance(zoo, list)
        assert len(zoo) > 0


class TestQuantOptimEdgeCases:
    def test_returns_from_price_matrix_empty(self) -> None:
        from app.foundation.quant_optim import _returns_from_price_matrix

        result = _returns_from_price_matrix({})
        assert len(result) == 0

    def test_run_optimisation_empty_matrix(self) -> None:
        from app.foundation.quant_optim import run_optimisation

        result = run_optimisation({})
        assert result.get("status") in ("unavailable", "failed")

