"""Tests for new quant functions: Newey-West SE, Black-Litterman, Kelly, Volatility Drag."""
import math

import numpy as np


# ---------------------------------------------------------------------------
# Newey-West Standard Errors
# ---------------------------------------------------------------------------

class TestNeweyWestSE:
    def test_returns_correct_keys(self):
        from app.foundation.quant_factors import newey_west_se

        rng = np.random.default_rng(42)
        n, k = 100, 3
        X = rng.normal(0, 1, (n, k))
        residuals = rng.normal(0, 0.01, n)

        result = newey_west_se(residuals, X)
        assert "hac_cov" in result
        assert "hac_se" in result
        assert "lags" in result
        assert "n" in result

    def test_hac_se_positive(self):
        from app.foundation.quant_factors import newey_west_se

        rng = np.random.default_rng(42)
        n, k = 100, 2
        X = rng.normal(0, 1, (n, k))
        residuals = rng.normal(0, 0.01, n)

        result = newey_west_se(residuals, X)
        for se in result["hac_se"]:
            assert se >= 0

    def test_hac_cov_symmetric(self):
        from app.foundation.quant_factors import newey_west_se

        rng = np.random.default_rng(42)
        n, k = 100, 2
        X = rng.normal(0, 1, (n, k))
        residuals = rng.normal(0, 0.01, n)

        result = newey_west_se(residuals, X)
        cov = np.array(result["hac_cov"])
        assert np.allclose(cov, cov.T)

    def test_custom_lags(self):
        from app.foundation.quant_factors import newey_west_se

        rng = np.random.default_rng(42)
        n, k = 100, 2
        X = rng.normal(0, 1, (n, k))
        residuals = rng.normal(0, 0.01, n)

        result = newey_west_se(residuals, X, lags=5)
        assert result["lags"] == 5

    def test_too_few_observations(self):
        from app.foundation.quant_factors import newey_west_se

        result = newey_west_se([0.01], [[1.0]])
        assert result["hac_cov"] is None
        assert result["hac_se"] is None

    def test_1d_array_input(self):
        from app.foundation.quant_factors import newey_west_se

        rng = np.random.default_rng(42)
        n = 100
        residuals = rng.normal(0, 0.01, n)
        X = rng.normal(0, 1, n)

        result = newey_west_se(residuals, X)
        assert len(result["hac_se"]) == 1


# ---------------------------------------------------------------------------
# Black-Litterman Optimizer
# ---------------------------------------------------------------------------

class TestKellyFraction:
    def test_positive_for_positive_expected_return(self):
        from app.foundation.quant_metrics import kelly_fraction

        returns = [0.01, 0.015, 0.008, 0.012, 0.011]
        kelly = kelly_fraction(returns)
        assert kelly > 0

    def test_zero_for_zero_excess_return(self):
        from app.foundation.quant_metrics import kelly_fraction

        returns = [0.0] * 100
        kelly = kelly_fraction(returns)
        assert kelly == 0.0

    def test_too_few_observations(self):
        from app.foundation.quant_metrics import kelly_fraction

        kelly = kelly_fraction([0.01])
        assert kelly == 0.0

    def test_negative_for_negative_expected_return(self):
        from app.foundation.quant_metrics import kelly_fraction

        returns = [-0.05, -0.04, -0.03, -0.02, -0.01]
        kelly = kelly_fraction(returns)
        assert kelly < 0


# ---------------------------------------------------------------------------
# Volatility Drag
# ---------------------------------------------------------------------------

class TestVolatilityDrag:
    def test_drag_non_negative(self):
        from app.foundation.quant_metrics import volatility_drag

        returns = [0.01, -0.02, 0.015, -0.01, 0.02]
        drag = volatility_drag(returns)
        assert drag >= 0

    def test_zero_for_constant_returns(self):
        from app.foundation.quant_metrics import volatility_drag

        returns = [0.001] * 100
        drag = volatility_drag(returns)
        assert math.isclose(drag, 0.0, abs_tol=1e-10)

    def test_drag_increases_with_volatility(self):
        from app.foundation.quant_metrics import volatility_drag

        low_vol = [0.001, 0.0011, 0.0009, 0.001, 0.0012]
        high_vol = [0.01, -0.01, 0.02, -0.02, 0.015]
        assert volatility_drag(high_vol) > volatility_drag(low_vol)

    def test_too_few_observations(self):
        from app.foundation.quant_metrics import volatility_drag

        drag = volatility_drag([0.01])
        assert drag == 0.0

    def test_annualisation(self):
        from app.foundation.quant_metrics import volatility_drag

        daily_returns = [0.001, -0.001, 0.002, -0.002, 0.001]
        drag_daily = volatility_drag(daily_returns, periods_per_year=252)
        drag_weekly = volatility_drag(daily_returns, periods_per_year=52)
        assert drag_daily > drag_weekly
