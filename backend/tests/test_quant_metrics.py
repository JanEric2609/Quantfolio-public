"""Quant metric golden values."""
import math
import random

from app.foundation import quant_metrics


def test_sharpe_zero_volatility_returns_zero():
    assert quant_metrics.sharpe_ratio([0.001] * 50) == 0.0


def test_sharpe_positive_for_consistently_positive_returns():
    returns = [0.001] * 200 + [0.002] * 200
    assert quant_metrics.sharpe_ratio(returns) > 0


def test_sortino_only_penalises_downside():
    upside = [0.01, 0.02, 0.015, 0.018, 0.012]
    only_up = quant_metrics.sortino_ratio(upside)
    # With no downside, the divisor is 0 → we return 0 by convention.
    assert only_up == 0.0
    mixed = [0.01, -0.02, 0.015, -0.01, 0.012]
    assert quant_metrics.sortino_ratio(mixed) != 0.0


def test_max_drawdown_for_known_series():
    # 1.0 → 1.10 → 0.88 → 0.99 → 1.05. Drawdown trough at 0.88 from peak 1.10 = -20%.
    returns = [0.10, -0.20, 0.125, 0.060606]
    dd = quant_metrics.max_drawdown(returns)
    assert math.isclose(dd["max_drawdown"], -0.20, rel_tol=1e-6, abs_tol=1e-6)


def test_calmar_positive_for_recovering_series():
    returns = [0.01] * 100 + [-0.05] + [0.01] * 100
    calmar = quant_metrics.calmar_ratio(returns)
    assert calmar > 0


def test_historical_cvar_is_the_empirical_tail_average():
    # Hand-computed, not regenerated from the implementation: with n=10 and
    # confidence 0.95, floor(0.05 * 10) = 0 selects the single worst
    # observation, so the expected shortfall is that observation's magnitude.
    returns = sorted([-0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04])
    cvar95 = quant_metrics.historical_cvar(returns, 0.95)
    assert math.isclose(cvar95, 0.05, abs_tol=1e-9)

    # At 80% confidence the tail widens to the worst two: mean(-0.05, -0.04).
    cvar80 = quant_metrics.historical_cvar(returns, 0.80)
    assert math.isclose(cvar80, 0.045, abs_tol=1e-9)


def test_historical_cvar_never_below_historical_var():
    """The invariant the two paired call sites depend on.

    Expected shortfall averages observations at or below the VaR threshold, so
    it can never come in under the threshold itself. The QuantStats-backed
    implementation this replaced could not guarantee this: it paired a
    parametric threshold with a historical average, and on small samples
    degenerated into returning the parametric VaR outright.
    """
    rng = random.Random(42)
    for _ in range(500):
        n = rng.choice([5, 20, 60, 250])
        series = [rng.gauss(0.0004, 0.012) for _ in range(n)]
        var = quant_metrics.historical_var(series, 0.95)
        cvar = quant_metrics.historical_cvar(series, 0.95)
        assert cvar >= var - 1e-12, (n, var, cvar)


def test_negative_risk_free_raises_sharpe():
    """A negative rf must lift excess returns, not be silently discarded.

    QuantStats gates its own ``rf=`` argument on ``if rf > 0``, so delegating
    the conversion to it dropped negative rates entirely — and euro rates were
    negative for most of 2015-2022, while ``risk_free`` is a user-supplied
    query parameter on ``GET /api/quant/portfolio/risk``.
    """
    rng = random.Random(7)
    series = [rng.gauss(0.0003, 0.01) for _ in range(500)]
    assert quant_metrics.sharpe_ratio(series, risk_free=-0.005) > quant_metrics.sharpe_ratio(
        series, risk_free=0.0
    )
    assert quant_metrics.sharpe_ratio(series, risk_free=0.005) < quant_metrics.sharpe_ratio(
        series, risk_free=0.0
    )


def test_parametric_cvar_positive():
    returns = [(-1) ** i * 0.01 for i in range(100)]
    cvar = quant_metrics.parametric_cvar(returns)
    assert cvar > 0


def test_beta_against_self_is_one():
    series = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01]
    assert math.isclose(quant_metrics.beta(series, series), 1.0, abs_tol=1e-9)


def test_beta_against_constant_benchmark_is_zero():
    portfolio = [0.01, -0.02, 0.015]
    benchmark = [0.0, 0.0, 0.0]
    assert quant_metrics.beta(portfolio, benchmark) == 0.0


def test_r_squared_perfect_correlation_is_one():
    series = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01]
    assert math.isclose(quant_metrics.r_squared(series, series), 1.0, abs_tol=1e-9)


def test_full_risk_report_keys():
    returns = [0.001 * (i % 5 - 2) for i in range(252)]
    bench = [0.001 * ((i + 1) % 5 - 2) for i in range(252)]
    report = quant_metrics.full_risk_report(returns, benchmark_returns=bench)
    for key in (
        "annualised_return",
        "annualised_volatility",
        "sharpe",
        "sortino",
        "calmar",
        "historical_cvar",
        "parametric_cvar",
        "drawdown",
        "beta",
        "alpha",
        "r_squared",
        "treynor",
    ):
        assert key in report


def test_skew_and_kurtosis_normal_ish():
    # Reasonably symmetric series → skew small.
    returns = [0.01 * math.sin(i / 7) for i in range(252)]
    assert abs(quant_metrics.skewness(returns)) < 0.5


# ---------------------------------------------------------------------------
# kelly_fraction
# ---------------------------------------------------------------------------


def test_kelly_fraction_positive_for_positive_returns():
    """Kelly fraction should be positive for consistently positive returns with variance."""
    rng = __import__('random').Random(42)
    returns = [0.01 + rng.gauss(0, 0.005) for _ in range(100)]
    kf = quant_metrics.kelly_fraction(returns)
    assert kf > 0


def test_kelly_fraction_zero_for_zero_variance():
    """Kelly fraction should be zero when variance is zero (constant returns)."""
    returns = [0.001] * 50
    assert quant_metrics.kelly_fraction(returns) == 0.0


def test_kelly_fraction_negative_for_negative_excess_returns():
    """Kelly fraction should be negative when expected return < risk-free rate."""
    rng = __import__('random').Random(42)
    returns = [-0.01 + rng.gauss(0, 0.01) for _ in range(100)]
    kf = quant_metrics.kelly_fraction(returns, risk_free=0.05)
    assert kf < 0


def test_kelly_fraction_single_observation():
    """Kelly fraction should return 0 for single observation."""
    assert quant_metrics.kelly_fraction([0.01]) == 0.0


# ---------------------------------------------------------------------------
# volatility_drag
# ---------------------------------------------------------------------------


def test_volatility_drag_non_negative():
    """Volatility drag is always non-negative."""
    returns = [0.01, -0.02, 0.015, -0.005, 0.02]
    drag = quant_metrics.volatility_drag(returns)
    assert drag >= 0


def test_volatility_drag_zero_for_constant_returns():
    """Zero volatility means zero drag."""
    returns = [0.001] * 100
    assert quant_metrics.volatility_drag(returns) == 0.0


def test_volatility_drag_increases_with_volatility():
    """Higher volatility should produce higher drag."""
    low_vol = [0.001, 0.002, 0.001, 0.002, 0.001]
    high_vol = [0.01, -0.02, 0.015, -0.01, 0.02]
    assert quant_metrics.volatility_drag(high_vol) > quant_metrics.volatility_drag(low_vol)


def test_volatility_drag_single_observation():
    """Single observation should return 0."""
    assert quant_metrics.volatility_drag([0.01]) == 0.0


# ---------------------------------------------------------------------------
# rolling_sharpe
# ---------------------------------------------------------------------------


def test_rolling_sharpe_length():
    """Rolling Sharpe should return len(returns) - window + 1 values."""
    returns = [0.001 * (i % 5 - 2) for i in range(100)]
    result = quant_metrics.rolling_sharpe(returns, window=20)
    assert len(result) == 81  # 100 - 20 + 1


def test_rolling_sharpe_empty_for_short_series():
    """Series shorter than window should return empty list."""
    returns = [0.01] * 10
    result = quant_metrics.rolling_sharpe(returns, window=20)
    assert result == []


def test_rolling_sharpe_values_are_finite():
    """All rolling Sharpe values should be finite."""
    returns = [0.001 * math.sin(i / 7) for i in range(100)]
    result = quant_metrics.rolling_sharpe(returns, window=20)
    for val in result:
        assert math.isfinite(val)


# ---------------------------------------------------------------------------
# annualised_return
# ---------------------------------------------------------------------------


def test_annualised_return_positive_for_positive_returns():
    """Annualised return should be positive for positive daily returns."""
    returns = [0.001] * 252  # 0.1% daily
    ann = quant_metrics.annualised_return(returns)
    assert ann > 0


def test_annualised_return_zero_for_empty():
    """Empty returns should give 0."""
    assert quant_metrics.annualised_return([]) == 0.0


def test_annualised_return_compounding():
    """Verify compounding: 10% daily for 1 day should give ~10% annualised."""
    returns = [0.10]  # Single day 10%
    # With 1 observation: exp(log(1.1) * 252/1) - 1
    expected = math.exp(math.log(1.1) * 252) - 1
    result = quant_metrics.annualised_return(returns, periods_per_year=252)
    assert math.isclose(result, expected, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# annualised_volatility
# ---------------------------------------------------------------------------


def test_annualised_volatility_zero_for_constant():
    """Constant returns should have zero volatility."""
    returns = [0.001] * 100
    assert quant_metrics.annualised_volatility(returns) == 0.0


def test_annualised_volatility_positive():
    """Volatile returns should have positive volatility."""
    returns = [0.01, -0.02, 0.015, -0.01, 0.02]
    vol = quant_metrics.annualised_volatility(returns)
    assert vol > 0


def test_annualised_volatility_single_observation():
    """Single observation should return 0."""
    assert quant_metrics.annualised_volatility([0.01]) == 0.0


# ---------------------------------------------------------------------------
# downside_deviation
# ---------------------------------------------------------------------------


def test_downside_deviation_zero_for_all_positive():
    """No negative returns means zero downside deviation."""
    returns = [0.01, 0.02, 0.015, 0.018, 0.012]
    assert quant_metrics.downside_deviation(returns) == 0.0


def test_downside_deviation_positive_with_negative_returns():
    """Negative returns should produce positive downside deviation."""
    returns = [0.01, -0.02, 0.015, -0.01, 0.012]
    dd = quant_metrics.downside_deviation(returns)
    assert dd > 0


# ---------------------------------------------------------------------------
# kurtosis
# ---------------------------------------------------------------------------


def test_kurtosis_normal_distribution():
    """Normal-ish distribution should have kurtosis near 0."""
    import random
    rng = random.Random(42)
    returns = [rng.gauss(0, 0.01) for _ in range(252)]
    kurt = quant_metrics.kurtosis(returns)
    assert abs(kurt) < 0.5


def test_kurtosis_single_observation():
    """Few observations should return 0."""
    assert quant_metrics.kurtosis([0.01]) == 0.0


def test_kurtosis_heavy_tailed():
    """Distribution with outliers should have positive excess kurtosis."""
    # Mix of normal and extreme values
    returns = [0.01] * 90 + [0.10] * 5 + [-0.10] * 5
    kurt = quant_metrics.kurtosis(returns)
    assert kurt > 0  # Heavy tails → positive excess kurtosis


# ---------------------------------------------------------------------------
# treynor_ratio
# ---------------------------------------------------------------------------


def test_treynor_ratio_positive_for_positive_portfolio():
    """Positive portfolio returns should give positive Treynor."""
    portfolio = [0.01, 0.02, 0.015, 0.02, 0.01]
    benchmark = [0.005, 0.01, 0.008, 0.012, 0.006]
    treynor = quant_metrics.treynor_ratio(portfolio, benchmark)
    assert treynor > 0


def test_treynor_ratio_zero_when_beta_zero():
    """Zero beta means zero Treynor."""
    portfolio = [0.01, -0.02, 0.015]
    benchmark = [0.0, 0.0, 0.0]
    assert quant_metrics.treynor_ratio(portfolio, benchmark) == 0.0


def test_treynor_ratio_aligns_windows_like_alpha():
    """Mismatched-length inputs must use the same trimmed window as beta/alpha.

    Regression for a bug where treynor_ratio computed beta() on the common
    trailing window (as beta() always does) but annualised_return() on the
    full untrimmed portfolio series — the exact inconsistency alpha() was
    explicitly patched to avoid. A long portfolio history paired with a
    short benchmark history must give the same result regardless of how
    much extra portfolio history precedes the benchmark's window.
    """
    tail_portfolio = [0.01, 0.02, 0.015]
    tail_benchmark = [0.005, 0.01, 0.008]
    baseline = quant_metrics.treynor_ratio(tail_portfolio, tail_benchmark)

    padded_portfolio = [0.05] * 47 + tail_portfolio
    padded = quant_metrics.treynor_ratio(padded_portfolio, tail_benchmark)

    assert math.isclose(padded, baseline, rel_tol=1e-9)


def test_risk_series_equity_and_drawdown_shapes():
    returns = [0.01, -0.02, 0.015, 0.0, -0.005]
    dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    out = quant_metrics.risk_series(returns, dates)
    assert len(out["equity_curve"]) == len(returns)
    assert len(out["rolling_drawdown"]) == len(returns)
    assert out["equity_curve"][0] == {"date": "2024-01-02", "value": round(100.0 * 1.01, 6)}
    # Drawdown is always <= 0 and 0 at a new peak.
    assert all(p["value"] <= 1e-9 for p in out["rolling_drawdown"])
    assert out["returns"] == [round(r, 8) for r in returns]
    # No benchmark -> no regression/rolling beta.
    assert out["regression_points"] == []
    assert out["rolling_beta"] == []


def test_risk_series_benchmark_regression_and_beta():
    returns = [0.01 * i for i in range(1, 80)]
    bench = [0.008 * i for i in range(1, 80)]
    dates = [f"d{i}" for i in range(len(returns))]
    out = quant_metrics.risk_series(returns, dates, benchmark_returns=bench, beta_window=20)
    # Aligned scatter pairs (benchmark, portfolio).
    assert len(out["regression_points"]) == len(returns)
    assert out["regression_points"][0] == [round(bench[0], 8), round(returns[0], 8)]
    assert len(out["rolling_beta"]) > 0
    assert all("date" in p and "value" in p for p in out["rolling_beta"])
