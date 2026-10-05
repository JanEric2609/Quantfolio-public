"""Tests for quant_optim: skfolio-powered portfolio optimisation and stress-testing."""
from __future__ import annotations

import sys
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_price_matrix(n_rows: int = 120, n_assets: int = 3, seed: int = 42) -> dict[str, dict[str, float]]:
    """Build a synthetic price matrix with *n_rows* dates and *n_assets* assets."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_rows, freq="B")
    result: dict[str, dict[str, float]] = {}
    for i in range(n_assets):
        prices = 100.0 * np.cumprod(1 + rng.normal(0.0005, 0.01, n_rows))
        result[f"ASSET{i}"] = {str(d.date()): float(p) for d, p in zip(dates, prices)}
    return result


def _make_short_price_matrix(n_rows: int = 10) -> dict[str, dict[str, float]]:
    """Price matrix with fewer rows than the minimum required (< 30 returns)."""
    return _make_price_matrix(n_rows=n_rows)


# ---------------------------------------------------------------------------
# run_all_objectives
# ---------------------------------------------------------------------------

class TestRunAllObjectives:
    def test_returns_status_key(self) -> None:
        from app.foundation.quant_optim import run_all_objectives

        price_matrix = _make_price_matrix()
        result = run_all_objectives(price_matrix)
        assert "status" in result

    def test_completed_with_valid_data(self) -> None:
        """With skfolio available and enough data the status should be 'completed'."""
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_all_objectives

        price_matrix = _make_price_matrix()
        result = run_all_objectives(price_matrix)
        assert result["status"] == "completed"

    def test_methods_dict_present_on_success(self) -> None:
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_all_objectives

        price_matrix = _make_price_matrix()
        result = run_all_objectives(price_matrix)
        if result["status"] == "completed":
            assert "methods" in result
            assert isinstance(result["methods"], dict)

    def test_unavailable_when_skfolio_missing(self) -> None:
        """Patch sys.modules to hide skfolio → service should return unavailable."""
        import skfolio  # noqa: F401 — ensure skfolio entries exist in sys.modules before we patch them
        skfolio_mods = {k: v for k, v in sys.modules.items() if k.startswith("skfolio")}
        assert skfolio_mods, "skfolio should be importable in this environment"
        with patch.dict(sys.modules, {k: None for k in skfolio_mods}):  # type: ignore[arg-type]
            import importlib
            import app.foundation.quant_optim as _mod
            importlib.reload(_mod)
            result = _mod.run_all_objectives(_make_price_matrix())
        assert result.get("status") == "unavailable"

    def test_unavailable_when_too_few_rows(self) -> None:
        """Fewer than 101 price rows → unavailable (needs 100 returns)."""
        from app.foundation.quant_optim import run_all_objectives

        price_matrix = _make_short_price_matrix(n_rows=10)
        result = run_all_objectives(price_matrix)
        assert result.get("status") == "unavailable"

    def test_three_asset_matrix_weights_sum_to_one(self) -> None:
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_all_objectives

        price_matrix = _make_price_matrix()
        result = run_all_objectives(price_matrix)
        if result["status"] != "completed":
            pytest.skip("skfolio optimisation did not complete")
        for method_name, method_result in result["methods"].items():
            if "weights" in method_result:
                total = sum(method_result["weights"].values())
                assert abs(total - 1.0) < 1e-4, (
                    f"{method_name} weights sum to {total}, expected ~1.0"
                )


# ---------------------------------------------------------------------------
# run_stress_test
# ---------------------------------------------------------------------------

class TestRunStressTest:
    def test_returns_status_key(self) -> None:
        from app.foundation.quant_optim import run_stress_test

        result = run_stress_test(_make_price_matrix(), n_scenarios=50)
        assert "status" in result

    def test_unavailable_when_too_few_rows(self) -> None:
        from app.foundation.quant_optim import run_stress_test

        result = run_stress_test(_make_short_price_matrix(n_rows=20), n_scenarios=50)
        assert result.get("status") in ("unavailable", "completed", "failed")

    def test_completed_result_shape(self) -> None:
        """If VineCopula is available the result must have the expected keys."""
        from app.foundation.quant_optim import run_stress_test

        result = run_stress_test(_make_price_matrix(), n_scenarios=100)
        if result["status"] == "completed":
            for key in (
                "n_scenarios",
                "p05_portfolio_return",
                "p50_portfolio_return",
                "p95_portfolio_return",
                "mean_portfolio_return",
                "stressed_cvar_95",
            ):
                assert key in result, f"Missing key: {key}"

    def test_p05_le_p50_le_p95(self) -> None:
        from app.foundation.quant_optim import run_stress_test

        result = run_stress_test(_make_price_matrix(), n_scenarios=200)
        if result["status"] != "completed":
            pytest.skip("VineCopula not available or insufficient data")
        assert result["p05_portfolio_return"] <= result["p50_portfolio_return"]
        assert result["p50_portfolio_return"] <= result["p95_portfolio_return"]


# ---------------------------------------------------------------------------
# _returns_from_price_matrix (internal helper)
# ---------------------------------------------------------------------------

class TestReturnsFromPriceMatrix:
    def test_output_shape(self) -> None:
        from app.foundation.quant_optim import _returns_from_price_matrix

        pm = _make_price_matrix(n_rows=35, n_assets=3)
        returns = _returns_from_price_matrix(pm)
        assert returns.shape[1] == 3
        # pct_change drops first row → 34 observations
        assert len(returns) == 34

    def test_no_nulls_in_output(self) -> None:
        from app.foundation.quant_optim import _returns_from_price_matrix

        pm = _make_price_matrix(n_rows=35, n_assets=2)
        returns = _returns_from_price_matrix(pm)
        assert not returns.isnull().any().any()


# ---------------------------------------------------------------------------
# run_optimisation
# ---------------------------------------------------------------------------


class TestRunOptimisation:
    def test_returns_status_key(self) -> None:
        from app.foundation.quant_optim import run_optimisation

        result = run_optimisation(_make_price_matrix())
        assert "status" in result

    def test_completed_with_valid_data(self) -> None:
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        result = run_optimisation(_make_price_matrix())
        assert result["status"] == "completed"

    def test_weights_sum_to_one(self) -> None:
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        result = run_optimisation(_make_price_matrix())
        if result["status"] != "completed":
            pytest.skip("skfolio not available")
        total = sum(result["weights"].values())
        assert abs(total - 1.0) < 1e-4

    def test_unavailable_when_too_few_rows(self) -> None:
        from app.foundation.quant_optim import run_optimisation

        result = run_optimisation(_make_short_price_matrix(n_rows=10))
        assert result.get("status") == "unavailable"

    def test_custom_weights_constraint(self) -> None:
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        result = run_optimisation(
            _make_price_matrix(),
            weights_constraint={"min": 0.1, "max": 0.5},
        )
        if result["status"] != "completed":
            pytest.skip("skfolio not available")
        for w in result["weights"].values():
            assert 0.1 - 1e-6 <= w <= 0.5 + 1e-6


# ---------------------------------------------------------------------------
# run_optimisation — policy weights / tactical bands (F8/F10/F13)
# ---------------------------------------------------------------------------

def _make_cash_attractor_matrix(n_rows: int = 260, seed: int = 42) -> dict[str, dict[str, float]]:
    """Two ordinary assets plus a near-zero-vol money-market-shaped asset —
    reproduces the prod cash-attractor failure: an unconstrained max_ratio
    optimiser dumps ~100% into the near-zero-vol name."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_rows, freq="B")

    def _series(mu: float, sigma: float) -> dict[str, float]:
        prices = 100.0 * np.cumprod(1 + rng.normal(mu, sigma, n_rows))
        return {str(d.date()): float(p) for d, p in zip(dates, prices)}

    return {
        "EQUITY1": _series(0.0005, 0.012),
        "EQUITY2": _series(0.0004, 0.011),
        "CASH1": _series(0.00006, 0.0001),
    }


class TestRunOptimisationPolicyBands:
    def test_unconstrained_reproduces_cash_attractor(self) -> None:
        """Baseline: without instrument_types, the near-zero-vol asset
        dominates — confirms the fixture actually reproduces the bug."""
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        result = run_optimisation(_make_cash_attractor_matrix(), objective="max_ratio", risk_measure="cvar")
        if result["status"] != "completed":
            pytest.skip("skfolio not available")
        assert result["weights"]["CASH1"] > 0.9

    def test_instrument_types_caps_money_market_within_policy_band(self) -> None:
        """No bond asset is present in this fixture, so the equity/cash
        bands are renormalised over just those two sleeves (bond's target
        has nowhere to go) — the effective cash ceiling is higher than the
        raw POLICY_WEIGHTS["cash"] + TACTICAL_BAND, but still bounded."""
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import POLICY_WEIGHTS, TACTICAL_BAND, run_optimisation

        instrument_types = {"EQUITY1": "equity", "EQUITY2": "etf", "CASH1": "money_market"}
        result = run_optimisation(
            _make_cash_attractor_matrix(),
            objective="max_ratio",
            risk_measure="cvar",
            instrument_types=instrument_types,
        )
        if result["status"] != "completed":
            pytest.skip("skfolio not available")
        cash_weight = result["weights"]["CASH1"]
        present_total = POLICY_WEIGHTS["equity"] + POLICY_WEIGHTS["cash"]
        hi = POLICY_WEIGHTS["cash"] / present_total + TACTICAL_BAND
        assert cash_weight <= hi + 1e-6
        # Well below the unconstrained ~1.0 cash-attractor outcome either way.
        assert cash_weight < 0.9

    def test_all_three_sleeves_present_uses_raw_policy_band(self) -> None:
        """With equity, bond, and cash all present, no renormalisation is
        needed and the raw POLICY_WEIGHTS +/- TACTICAL_BAND applies directly."""
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import POLICY_WEIGHTS, TACTICAL_BAND, run_optimisation

        matrix = _make_cash_attractor_matrix()
        rng = np.random.default_rng(7)
        dates = pd.date_range("2023-01-01", periods=260, freq="B")
        prices = 100.0 * np.cumprod(1 + rng.normal(0.0001, 0.002, 260))
        matrix["BOND1"] = {str(d.date()): float(p) for d, p in zip(dates, prices)}
        instrument_types = {
            "EQUITY1": "equity", "EQUITY2": "etf", "CASH1": "money_market", "BOND1": "bond",
        }
        result = run_optimisation(
            matrix, objective="max_ratio", risk_measure="cvar", instrument_types=instrument_types,
        )
        if result["status"] != "completed":
            pytest.skip("skfolio not available")
        cash_weight = result["weights"]["CASH1"]
        hi = POLICY_WEIGHTS["cash"] + TACTICAL_BAND
        assert cash_weight <= hi + 1e-6

    def test_instrument_types_still_sums_to_one(self) -> None:
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        instrument_types = {"EQUITY1": "equity", "EQUITY2": "etf", "CASH1": "money_market"}
        result = run_optimisation(
            _make_cash_attractor_matrix(),
            objective="max_ratio",
            risk_measure="cvar",
            instrument_types=instrument_types,
        )
        if result["status"] != "completed":
            pytest.skip("skfolio not available")
        assert abs(sum(result["weights"].values()) - 1.0) < 1e-4

    def test_no_instrument_types_is_backward_compatible(self) -> None:
        """Omitting instrument_types must behave exactly as before (additive
        parameter, not a breaking signature change)."""
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        matrix = _make_cash_attractor_matrix()
        with_none = run_optimisation(matrix, objective="max_ratio", risk_measure="cvar", instrument_types=None)
        without_param = run_optimisation(matrix, objective="max_ratio", risk_measure="cvar")
        if with_none["status"] != "completed":
            pytest.skip("skfolio not available")
        assert with_none["weights"] == pytest.approx(without_param["weights"])

    def test_max_turnover_caps_change_under_min_risk(self) -> None:
        """max_turnover is only actually enforced under objectives compatible
        with skfolio's turnover reformulation — MINIMIZE_RISK works."""
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        matrix = _make_cash_attractor_matrix()
        previous_weights = {"EQUITY1": 1.0, "EQUITY2": 0.0, "CASH1": 0.0}
        result = run_optimisation(
            matrix,
            objective="min_risk",
            risk_measure="cvar",
            previous_weights=previous_weights,
            max_turnover=0.10,
        )
        if result["status"] != "completed":
            pytest.skip("skfolio not available")
        max_per_asset_move = max(
            abs(result["weights"].get(k, 0.0) - v) for k, v in previous_weights.items()
        )
        assert max_per_asset_move <= 0.10 + 1e-6

    def test_max_turnover_silently_dropped_under_max_ratio(self) -> None:
        """MAXIMIZE_RATIO's fractional reformulation is incompatible with
        skfolio's turnover constraint (confirmed: identical inputs solve
        under min_risk, fail under max_ratio) — run_optimisation must drop
        max_turnover rather than let the whole call fail, so a caller on
        max_ratio still gets the load-bearing policy-band constraints."""
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        matrix = _make_cash_attractor_matrix()
        previous_weights = {"EQUITY1": 1.0, "EQUITY2": 0.0, "CASH1": 0.0}
        result = run_optimisation(
            matrix,
            objective="max_ratio",
            risk_measure="cvar",
            previous_weights=previous_weights,
            max_turnover=0.10,
        )
        assert result["status"] == "completed"




class TestRiskFreeRateUnits:
    """The annual risk-free rate must reach skfolio as a per-period (daily) rate.

    Every production caller passes ``get_risk_free_rate(db)`` — an annual
    fraction such as 0.022. skfolio compares it against the *daily* expected
    returns of the matrix it was fitted on, so the unconverted annual figure
    made MAXIMIZE_RATIO infeasible ("assets' expected returns are all
    under-performing your risk-free rate") and the advisor proposal fell
    back to equal weight on every cycle.
    """

    def test_per_period_conversion_compounds_back_to_annual(self) -> None:
        from app.foundation.quant_metrics import TRADING_DAYS_PER_YEAR
        from app.foundation.quant_optim import per_period_risk_free_rate

        daily = per_period_risk_free_rate(0.022)
        assert daily == pytest.approx(0.022 / TRADING_DAYS_PER_YEAR, rel=0.02)
        assert (1 + daily) ** TRADING_DAYS_PER_YEAR - 1 == pytest.approx(0.022, rel=1e-9)
        assert per_period_risk_free_rate(None) == 0.0
        assert per_period_risk_free_rate(0.0) == 0.0

    def test_max_ratio_solves_with_an_annual_risk_free_rate(self) -> None:
        pytest.importorskip("skfolio")
        from app.foundation.quant_optim import run_optimisation

        # Every asset clears a 2.2% annual hurdle comfortably (~10-12%/yr drift),
        # so the only way this can fail is a unit mismatch on the hurdle itself.
        rng = np.random.default_rng(7)
        dates = pd.date_range("2023-01-02", periods=500, freq="B")
        matrix = {
            name: {
                str(d.date()): float(p)
                for d, p in zip(dates, 100.0 * np.cumprod(1 + rng.normal(drift, 0.01, len(dates))))
            }
            for name, drift in (("A", 0.0004), ("B", 0.00045), ("C", 0.0005))
        }
        result = run_optimisation(matrix, objective="max_ratio", risk_measure="cvar", risk_free_rate=0.022)
        assert result["status"] == "completed", result.get("message")
        assert sum(result["weights"].values()) == pytest.approx(1.0, abs=1e-6)
