"""Tests for the restricted AlphaCrafter factor DSL."""

import numpy as np
import pandas as pd
import pytest

from app.lab.alphacrafter.factor_dsl import (
    FactorDSLError,
    evaluate,
    validate,
)


def _panel() -> dict[str, pd.DataFrame]:
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    symbols = ["AAA", "BBB", "CCC"]
    rng = np.random.default_rng(0)
    close = pd.DataFrame(
        rng.uniform(10, 100, size=(10, 3)), index=idx, columns=symbols
    )
    volume = pd.DataFrame(
        rng.uniform(1e6, 5e6, size=(10, 3)), index=idx, columns=symbols
    )
    pe_ratio = pd.DataFrame(
        rng.uniform(5, 40, size=(10, 3)), index=idx, columns=symbols
    )
    return {"close": close, "volume": volume, "pe_ratio": pe_ratio}


# --- Safety: rejection of disallowed constructs --------------------------------


@pytest.mark.parametrize(
    "formula",
    [
        "__import__('os')",
        "close.__class__",
        "close.values",
        "open('/etc/passwd')",
        "eval('1+1')",
        "exec('x=1')",
        "[c for c in close]",
        "lambda x: x",
        "close[0]",
        "unknown_var + close",
        "rolling_mean(close)",  # wrong arity
        "rank(close, 5)",  # wrong arity
        "close > 1 > 0",  # chained comparison
        "True",  # boolean constant
    ],
)
def test_rejects_unsafe_or_invalid(formula):
    with pytest.raises(FactorDSLError):
        validate(formula)
    with pytest.raises(FactorDSLError):
        evaluate(formula, _panel())


def test_rejects_attribute_access_even_on_whitelisted_name():
    with pytest.raises(FactorDSLError):
        validate("close.rolling")


# --- Correctness of whitelisted expressions ------------------------------------


def test_arithmetic_matches_pandas():
    panel = _panel()
    result = evaluate("close / pe_ratio", panel)
    expected = panel["close"] / panel["pe_ratio"]
    pd.testing.assert_frame_equal(result, expected)


def test_delta_matches_shift():
    panel = _panel()
    result = evaluate("delta(close, 2)", panel)
    expected = panel["close"] - panel["close"].shift(2)
    pd.testing.assert_frame_equal(result, expected)


def test_rank_is_cross_sectional():
    panel = _panel()
    result = evaluate("rank(close)", panel)
    expected = panel["close"].rank(axis=1, pct=True)
    pd.testing.assert_frame_equal(result, expected)
    # Every row's ranks lie in (0, 1].
    assert (result.to_numpy() <= 1.0).all()
    assert (result.to_numpy() > 0.0).all()


def test_zscore_centers_each_row():
    panel = _panel()
    result = evaluate("zscore(close)", panel)
    # Each date's cross-sectional mean should be ~0.
    row_means = result.mean(axis=1)
    assert np.allclose(row_means.to_numpy(), 0.0, atol=1e-9)


def test_rolling_mean_smooths():
    panel = _panel()
    result = evaluate("rolling_mean(close, 3)", panel)
    expected = panel["close"].rolling(3, min_periods=1).mean()
    pd.testing.assert_frame_equal(result, expected)


def test_comparison_returns_numeric_frame():
    panel = _panel()
    result = evaluate("close > rolling_mean(close, 3)", panel)
    assert set(np.unique(result.to_numpy())).issubset({0.0, 1.0})


def test_nested_composition():
    panel = _panel()
    # A realistic momentum-style factor.
    result = evaluate("rank(delta(log(close), 5))", panel)
    assert result.shape == panel["close"].shape


def test_validate_accepts_all_functions():
    for formula in [
        "rank(close)",
        "zscore(volume)",
        "rolling_mean(close, 5)",
        "rolling_std(close, 5)",
        "rolling_sum(volume, 5)",
        "shift(close, 1)",
        "delta(close, 5)",
        "log(close)",
        "abs(close - shift(close, 1))",
        "ts_min(low, 10)" if False else "ts_min(close, 10)",
        "ts_max(close, 10)",
        "corr(close, volume, 10)",
    ]:
        validate(formula)
