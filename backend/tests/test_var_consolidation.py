"""Golden-value tests for the empirical-VaR consolidation (plan todo 27).

Step-0 capture-and-compare (r1 Oracle finding #5, ADR 0003 decision 1):
BEFORE any refactor, the SAME fixed synthetic series was pushed through both
the legacy ``quant.py`` implementation (``numpy.percentile(...,
method="lower")``, floor index ``floor((1-c)*(n-1))``) and the canonical
``quant_metrics.historical_var`` (order statistic, floor index
``floor((1-c)*n)``, n<2 -> 0.0).

Result on this series (n=100): the two DIVERGE — they select neighbouring
order statistics (sorted[4] vs sorted[5] at 95%; sorted[0] vs sorted[1] at
99%). Per ADR 0003 decision 1 the canonical implementation wins and the
golden values below were moved to the canonical outputs DELIBERATELY (a
documented convention change, noted in the commit body — never silently).
"""

import math

from app.foundation import quant as quant_module
from app.foundation.backtest_strategy import _historical_var as backtest_var
from app.foundation.quant_metrics import historical_var as canonical_var
from app.decision.verification.risk import _historical_var as risk_var

# Fixed synthetic series: deterministic arithmetic generator (no RNG, no
# seeding), 100 mixed gain/loss observations with a mildly fat left tail.
STEP0_SERIES = [
    (((i * 37) % 101) / 101.0 - 0.5) * 0.04 + math.sin(i * 1.7) * 0.004
    for i in range(1, 101)
]

# Step-0 recorded values (captured pre-refactor, see module docstring).
STEP0_LEGACY_QUANT_PY_VAR_95 = 0.018170705728259174  # percentile-lower convention
STEP0_LEGACY_QUANT_PY_VAR_99 = 0.01974286268970697
STEP0_CANONICAL_VAR_95 = 0.017647712349112037  # order-statistic convention (canonical)
STEP0_CANONICAL_VAR_99 = 0.019407877291375608


def _legacy_percentile_lower_var(returns: list[float], confidence: float) -> float:
    """Re-implementation of the retired quant.py convention, for documentation
    only: numpy.percentile(..., method="lower") floored loss magnitude."""
    import numpy as np

    if not returns:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    cutoff = np.percentile(arr, (1 - confidence) * 100, method="lower")
    return float(max(0.0, -cutoff))


class TestStep0DivergenceDocumented:
    def test_legacy_and_canonical_conventions_diverge_on_step0_series(self):
        """The two tail conventions genuinely differ on the fixed series.

        If this ever fails because the conventions converged upstream, the
        golden constants in this file must be re-captured — not ignored.
        """
        legacy = _legacy_percentile_lower_var(STEP0_SERIES, 0.95)
        canonical = canonical_var(STEP0_SERIES, 0.95)
        assert legacy == STEP0_LEGACY_QUANT_PY_VAR_95
        assert canonical == STEP0_CANONICAL_VAR_95
        assert legacy != canonical

    def test_step0_canonical_golden_values_reproduce(self):
        assert canonical_var(STEP0_SERIES, 0.95) == STEP0_CANONICAL_VAR_95
        assert canonical_var(STEP0_SERIES, 0.99) == STEP0_CANONICAL_VAR_99


class TestQuantPyDelegation:
    """quant.historical_var keeps its public name/signature but must produce
    exactly the canonical values post-refactor (ADR 0003 decision 1)."""

    def test_var95_matches_canonical_golden(self):
        assert quant_module.historical_var(STEP0_SERIES, 0.95) == STEP0_CANONICAL_VAR_95

    def test_var99_matches_canonical_golden(self):
        assert quant_module.historical_var(STEP0_SERIES, 0.99) == STEP0_CANONICAL_VAR_99

    def test_default_confidence_is_95(self):
        assert quant_module.historical_var(STEP0_SERIES) == STEP0_CANONICAL_VAR_95


class TestVerificationRiskDelegation:
    def test_var95_matches_canonical_golden(self):
        assert risk_var(STEP0_SERIES, 0.95) == STEP0_CANONICAL_VAR_95

    def test_var99_matches_canonical_golden(self):
        assert risk_var(STEP0_SERIES, 0.99) == STEP0_CANONICAL_VAR_99

    def test_none_entries_filtered_like_canonical(self):
        polluted = STEP0_SERIES[:50] + [None] + STEP0_SERIES[50:]
        assert risk_var(polluted, 0.95) == STEP0_CANONICAL_VAR_95


class TestBacktestStrategyDelegation:
    """ADR 0003 decisions 1 and 6 override the former 'deliberate isolation'."""

    def test_var95_matches_canonical_golden(self):
        assert backtest_var(STEP0_SERIES, 0.95) == STEP0_CANONICAL_VAR_95

    def test_var99_matches_canonical_golden(self):
        assert backtest_var(STEP0_SERIES, 0.99) == STEP0_CANONICAL_VAR_99


class TestEdgeCaseConventions:
    """Degenerate-input contract of the canonical implementation."""

    def test_empty_series_is_zero_everywhere(self):
        assert quant_module.historical_var([], 0.95) == 0.0
        assert risk_var([], 0.95) == 0.0
        assert backtest_var([], 0.95) == 0.0

    def test_single_observation_is_zero_documented_change(self):
        # Documented convention change (ADR 0003 decision 1): a lone data
        # point is not a distribution tail. The legacy copies reported the
        # observation itself (e.g. 0.05 for [-0.05]).
        assert quant_module.historical_var([-0.05], 0.95) == 0.0
        assert risk_var([-0.05], 0.95) == 0.0
        assert backtest_var([-0.05], 0.95) == 0.0

    def test_all_gains_series_has_no_loss(self):
        gains = [0.01, 0.02, 0.03, 0.04, 0.05]
        assert quant_module.historical_var(gains, 0.95) == 0.0
        assert risk_var(gains, 0.95) == 0.0
        assert backtest_var(gains, 0.95) == 0.0

    def test_cvar_never_below_var_by_construction(self):
        from app.foundation.quant_metrics import historical_cvar

        var = canonical_var(STEP0_SERIES, 0.95)
        cvar = historical_cvar(STEP0_SERIES, 0.95)
        assert cvar >= var
