"""Tests for the unified expected-return anchor selector (Phase 3 of
docs/archive/plans/unified-portfolio-engine-implementation.md)."""
from __future__ import annotations

import pytest

from app.decision.discover.dossier_writer import (
    MZ_MIN_ROWS_BY_TYPE,
    SHRINKAGE_BY_INSTRUMENT_TYPE,
)
from app.foundation.expected_return import _ANCHOR_PRIORITY, select_return_anchor


def test_money_market_prefers_trailing_1m_over_3y_cagr():
    anchor = select_return_anchor(
        "money_market",
        {
            "trailing_3y_annualized_return": 0.0301,
            "trailing_1m_annualized_return": 0.021,
            "momentum_12_1m": 0.0185,
        },
    )
    assert anchor == {"value": 0.021, "method": "trailing_1m_annualized_return", "horizon": "1m"}


def test_money_market_falls_back_to_momentum_when_no_trailing_1m():
    anchor = select_return_anchor(
        "money_market",
        {"trailing_3y_annualized_return": 0.0301, "momentum_12_1m": 0.0185},
    )
    assert anchor == {"value": 0.0185, "method": "momentum_12_1m", "horizon": "12m"}


@pytest.mark.parametrize("instrument_type", ["equity", "etf"])
def test_equity_and_etf_prefer_trailing_3y_cagr(instrument_type):
    anchor = select_return_anchor(
        instrument_type,
        {
            "trailing_3y_annualized_return": 0.15,
            "trailing_1m_annualized_return": 0.60,
            "momentum_12_1m": 0.05,
        },
    )
    assert anchor == {"value": 0.15, "method": "trailing_3y_annualized_return", "horizon": "3y"}


def test_equity_falls_back_to_momentum_when_no_3y_cagr():
    anchor = select_return_anchor("equity", {"momentum_12_1m": 0.05})
    assert anchor == {"value": 0.05, "method": "momentum_12_1m", "horizon": "12m"}


def test_returns_none_when_no_candidate_has_a_value():
    assert select_return_anchor("equity", {}) is None
    assert select_return_anchor("money_market", {"trailing_3y_annualized_return": 0.03}) is None


def test_unknown_instrument_type_defaults_to_equity_priority():
    anchor = select_return_anchor("commodity", {"trailing_3y_annualized_return": 0.04, "momentum_12_1m": 0.02})
    assert anchor == {"value": 0.04, "method": "trailing_3y_annualized_return", "horizon": "3y"}


def test_bond_prefers_trailing_3y_then_momentum():
    """O4: bond funds get an EXPLICIT anchor priority mirroring the ETF one.

    Documented choice (discover-maths audit O4): duration-bearing bond funds
    still compound, so the long trailing window leads and 12-1m momentum is
    the fallback — same shape as etf/equity. The explicit ``"bond"`` key is
    asserted because the silent equity-priority fallback would otherwise
    produce identical values while hiding the classification gap.
    """
    assert "bond" in _ANCHOR_PRIORITY
    assert _ANCHOR_PRIORITY["bond"] == _ANCHOR_PRIORITY["etf"]

    anchor = select_return_anchor(
        "bond",
        {
            "trailing_3y_annualized_return": 0.04,
            "trailing_1m_annualized_return": 0.60,
            "momentum_12_1m": 0.02,
        },
    )
    assert anchor == {"value": 0.04, "method": "trailing_3y_annualized_return", "horizon": "3y"}

    fallback = select_return_anchor("bond", {"momentum_12_1m": 0.02})
    assert fallback == {"value": 0.02, "method": "momentum_12_1m", "horizon": "12m"}


def test_bond_shrinkage_and_mz_min_rows_constants_locked():
    """O4d: lock the dossier_writer constants the bond path relies on.

    A bond fund's trailing CAGR mean-reverts like an equity's, so it takes
    the hard equity-style shrinkage (0.10), not the money-market shallow
    shrinkage; the dynamic MZ fit gate stays at the pooled bar of 20 rows
    until per-class volume data justifies raising it.
    """
    assert SHRINKAGE_BY_INSTRUMENT_TYPE["bond"] == 0.10
    assert MZ_MIN_ROWS_BY_TYPE["bond"] == 20
