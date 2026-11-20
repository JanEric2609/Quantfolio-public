"""Tests for the risk gate's remediation loop, specifically GROWTH_FLOOR (F10).

Dropping names by worst MC P5 tail always removes the riskiest — i.e. most
growth-shaped — names first, since a money-market name's P5 is closest to
zero. Unchecked, a breach on a handful of volatile growth names can whittle
the book down to an all-cash-like remainder, reproducing the cash-attractor
failure Workstream 4 fixes at the optimiser level. GROWTH_FLOOR keeps
remediation from doing the same thing through a different path.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.decision.advisor.decision import McSummary, TradeProposal
from app.decision.advisor.risk_gate import GROWTH_FLOOR, check_risk_gate


def _series(mu: float, sigma: float, n: int = 260, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    prices = 100.0 * np.cumprod(1 + rng.normal(mu, sigma, n))
    return pd.Series(prices, index=dates)


def _mc(symbol: str, itype: str, p5: float) -> McSummary:
    return McSummary(symbol=symbol, instrument_type=itype, p5=p5, p50=0.02, p95=0.10, prob_positive=0.55, spot=100.0)


def _make_breaching_book() -> tuple[TradeProposal, dict[str, pd.Series]]:
    """Two volatile equities heavily weighted (breaches VaR ceiling) plus a
    near-zero-vol money-market name."""
    series_by_symbol = {
        "EQ1": _series(0.0002, 0.035, seed=1),
        "EQ2": _series(0.0002, 0.032, seed=2),
        "CASH1": _series(0.00006, 0.0001, seed=3),
    }
    weights = {"EQ1": 0.45, "EQ2": 0.45, "CASH1": 0.10}
    mc_summaries = {
        "EQ1": _mc("EQ1", "equity", p5=-0.09),
        "EQ2": _mc("EQ2", "equity", p5=-0.08),
        "CASH1": _mc("CASH1", "money_market", p5=-0.0005),
    }
    proposal = TradeProposal(
        portfolio_id="p1",
        suggested_weights=weights,
        mc_summaries=mc_summaries,
        risk_envelope={"var_95_daily": 0.05, "cvar_95_daily": 0.07, "max_drawdown": 0.30},
        current_book={},
        optimizer_status="completed",
        price_series=series_by_symbol,
        instrument_types={"EQ1": "equity", "EQ2": "equity", "CASH1": "money_market"},
    )
    return proposal, series_by_symbol


class TestGrowthFloor:
    def test_without_instrument_types_drops_growth_names_first(self):
        """Baseline (backward-compatible): omitting instrument_types
        reproduces the old unprotected behaviour — riskiest (growth) names
        get dropped first, potentially down to nothing but cash."""
        proposal, series_by_symbol = _make_breaching_book()
        result = check_risk_gate(proposal, series_by_symbol=series_by_symbol)
        dropped = {b["symbol"] for b in result.blocked}
        # At least one growth name was dropped before cash, since EQ1/EQ2
        # have the worst (most negative) P5.
        assert "EQ1" in dropped or "EQ2" in dropped

    def test_with_instrument_types_protects_growth_floor(self):
        proposal, series_by_symbol = _make_breaching_book()
        result = check_risk_gate(
            proposal,
            series_by_symbol=series_by_symbol,
            instrument_types=proposal.instrument_types,
        )
        remaining_growth = sum(
            proposal.suggested_weights[s]
            for s in result.passing_symbols
            if proposal.instrument_types.get(s) != "money_market"
        )
        original_total = sum(proposal.suggested_weights.values())
        assert remaining_growth >= GROWTH_FLOOR * original_total - 1e-6

    def test_growth_floor_is_a_positive_fraction_below_one(self):
        assert 0.0 < GROWTH_FLOOR < 1.0

    def test_no_breach_leaves_everything_untouched(self):
        proposal, series_by_symbol = _make_breaching_book()
        proposal.risk_envelope = {"var_95_daily": 0.001, "cvar_95_daily": 0.001, "max_drawdown": 0.01}
        result = check_risk_gate(
            proposal,
            series_by_symbol=series_by_symbol,
            instrument_types=proposal.instrument_types,
        )
        assert result.passed is True
        assert result.blocked == []
