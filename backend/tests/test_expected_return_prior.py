"""ADR 0017 — market-implied prior and credibility weight (pure functions)."""
from __future__ import annotations

import pytest

from app.foundation.expected_return import (
    EQUITY_PREMIUM_OVER_CASH,
    blume_adjusted_beta,
    credibility_weight,
    equilibrium_prior,
)


def test_blume_adjustment_pulls_toward_one():
    assert blume_adjusted_beta(1.0) == pytest.approx(1.0)
    assert blume_adjusted_beta(1.6) == pytest.approx(1.4)
    assert blume_adjusted_beta(0.4) == pytest.approx(0.6)


def test_blume_adjustment_clamps_nonsense_betas():
    assert blume_adjusted_beta(-0.8) == pytest.approx(1 / 3)
    assert blume_adjusted_beta(9.0) == pytest.approx(2 / 3 * 2.5 + 1 / 3)


def test_equity_prior_is_cash_plus_adjusted_beta_times_premium():
    prior, beta_used = equilibrium_prior("equity", 0.0244, 1.10)
    assert beta_used is not None
    assert beta_used == pytest.approx(2 / 3 * 1.10 + 1 / 3)
    assert prior == pytest.approx(0.0244 + beta_used * EQUITY_PREMIUM_OVER_CASH)


def test_missing_beta_counts_as_market():
    prior, beta_used = equilibrium_prior("etf", 0.02, None)
    assert beta_used == 1.0
    assert prior == pytest.approx(0.02 + EQUITY_PREMIUM_OVER_CASH)


def test_money_market_prior_is_cash():
    assert equilibrium_prior("money_market", 0.0244, 0.9) == (0.0244, None)


def test_bond_prior_uses_raw_beta_without_blume():
    prior, beta_used = equilibrium_prior("bond", 0.02, 0.05)
    assert beta_used == pytest.approx(0.05)
    assert prior == pytest.approx(0.02 + 0.05 * EQUITY_PREMIUM_OVER_CASH)


def test_credibility_weight_falls_with_volatility_and_rises_with_window():
    calm = credibility_weight(0.15, 3.0, cap=1.0)
    volatile = credibility_weight(0.40, 3.0, cap=1.0)
    longer = credibility_weight(0.40, 10.0, cap=1.0)
    assert calm > volatile
    assert longer > volatile
    assert volatile == pytest.approx(0.03**2 / (0.03**2 + 0.40**2 / 3.0))


def test_credibility_weight_capped_and_falls_back_to_cap():
    assert credibility_weight(0.02, 3.0, cap=0.10) == pytest.approx(0.10)
    assert credibility_weight(None, 3.0, cap=0.10) == 0.10
    assert credibility_weight(0.3, None, cap=0.10) == 0.10
    assert credibility_weight(0.0, 3.0, cap=0.10) == 0.10
