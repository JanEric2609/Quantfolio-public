"""Tests for DossierBuilder with Shared Memory H integration."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.lab.alphacrafter.dossier import DossierBuilder, compute_conviction
from app.lab.alphacrafter.shared_memory import (
    FactorMetrics,
    FactorState,
    SharedMemoryH,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def H_with_trader_outputs():
    """H with trader outputs for dossier testing."""
    H = SharedMemoryH.create_initial(
        universe=["SPY", "AAPL"],
        start_date="2021-01-01",
        end_date="2026-06-01",
    )
    H.trader_outputs = {
        "configs": [
            {
                "rebalance_freq": "W",
                "position_size": 0.1,
                "commission": 0.001,
                "sharpe_ratio": 1.45,
                "max_drawdown": -0.12,
                "total_return": 0.85,
                "n_factors": 2,
            }
        ],
        "n_configs_run": 1,
        "n_factors": 2,
    }
    H.screener_outputs = {
        "selected_factor_ids": "f1,f2",
        "regime_score": 0.10,
        "regime_label": "bull",
        "crisis": False,
    }
    H.factor_states = [
        FactorState(
            id="f1",
            name="momentum_12_1",
            source="seed",
            category="momentum",
            formula="close / shift(close, 252) - 1",
            dsl=None,
            metrics=FactorMetrics(ic=0.05, icir=0.8, turnover=0.4, decay_halflife_days=120, n_obs=1250),
            regime_applicability={"bull": 1.3},
        ),
        FactorState(
            id="f2",
            name="value_bp",
            source="seed",
            category="value",
            formula="1 / price_to_book",
            dsl=None,
            metrics=FactorMetrics(ic=0.04, icir=0.6, turnover=0.3, decay_halflife_days=None, n_obs=1250),
            regime_applicability={"bull": 1.1},
        ),
    ]
    return H


@pytest.fixture
def H_empty():
    """H with no trader outputs (empty pipeline)."""
    H = SharedMemoryH.create_initial(
        universe=["SPY"],
        start_date="2021-01-01",
        end_date="2026-06-01",
    )
    return H


@pytest.fixture
def H_with_crisis():
    """H with crisis flag set."""
    H = SharedMemoryH.create_initial(
        universe=["SPY", "AAPL"],
        start_date="2021-01-01",
        end_date="2026-06-01",
        crisis=True,
    )
    H.trader_outputs = {
        "configs": [
            {
                "rebalance_freq": "W",
                "position_size": 0.1,
                "commission": 0.001,
                "sharpe_ratio": 2.0,
                "max_drawdown": -0.25,
                "total_return": 0.60,
                "n_factors": 1,
            }
        ],
        "n_configs_run": 1,
        "n_factors": 1,
    }
    H.screener_outputs = {
        "selected_factor_ids": "f1",
        "regime_score": 0.08,
        "regime_label": "bear",
        "crisis": True,
    }
    H.factor_states = [
        FactorState(
            id="f1",
            name="momentum_12_1",
            source="seed",
            category="momentum",
            formula="close / shift(close, 252) - 1",
            dsl=None,
            metrics=FactorMetrics(ic=0.06, icir=0.9, turnover=0.5, decay_halflife_days=90, n_obs=1000),
            regime_applicability={"bear": 0.6},
        ),
    ]
    return H


# ---------------------------------------------------------------------------
# DossierBuilder class tests
# ---------------------------------------------------------------------------


class TestDossierBuilderReadsFromH:
    """DossierBuilder should read from H instead of individual arguments."""

    def test_build_returns_shared_memory_h(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        assert isinstance(result, SharedMemoryH)

    def test_build_writes_evaluation_to_h(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        assert result.evaluation != {}
        assert "dossiers_created" in result.evaluation

    def test_build_appends_to_agent_history(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        initial_history_len = len(H_with_trader_outputs.agent_history)
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        assert len(result.agent_history) > initial_history_len
        last_entry = result.agent_history[-1]
        assert last_entry["agent"] == "dossier"
        assert last_entry["action"] == "build"

    def test_build_reads_trader_outputs(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        assert "conviction_scores" in result.evaluation

    def test_build_reads_screener_outputs(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        assert result.evaluation.get("regime_label") == "bull"

    def test_build_reads_factor_states(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        assert "factor_names" in result.evaluation
        assert "momentum_12_1" in result.evaluation["factor_names"]
        assert "value_bp" in result.evaluation["factor_names"]


class TestDossierBuilderConviction:
    """DossierBuilder should compute conviction from H metrics."""

    def test_conviction_computed_from_sharpe(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        expected = compute_conviction(sharpe=1.45, mean_ic=0.045, regime_score=0.10)
        assert abs(result.evaluation["conviction_scores"]["SPY"] - expected) < 1e-6

    def test_conviction_uses_regime_score(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        expected = compute_conviction(
            sharpe=1.45, mean_ic=0.045, regime_score=0.10
        )
        assert abs(result.evaluation["conviction_scores"]["SPY"] - expected) < 1e-6

    def test_crisis_haircut_applied(self, H_with_crisis):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_crisis, db))
        expected = compute_conviction(
            sharpe=2.0, mean_ic=0.06, regime_score=0.08, crisis=True
        )
        assert abs(result.evaluation["conviction_scores"]["SPY"] - expected) < 1e-6

    def test_conviction_capped_at_0_95(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_with_trader_outputs, db))
        for symbol, score in result.evaluation["conviction_scores"].items():
            assert score <= 0.95, f"Conviction {score} for {symbol} exceeds 0.95"


class TestDossierBuilderEmpty:
    """DossierBuilder handles empty H gracefully."""

    def test_empty_trader_outputs_no_dossiers(self, H_empty):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_empty, db))
        assert result.evaluation.get("dossiers_created", 0) == 0

    def test_empty_trader_outputs_appends_history(self, H_empty):
        builder = DossierBuilder()
        db = MagicMock()
        result = asyncio.run(builder.build(H_empty, db))
        assert len(result.agent_history) > 0
        last_entry = result.agent_history[-1]
        assert last_entry["agent"] == "dossier"


class TestDossierBuilderPersistence:
    """DossierBuilder should persist RecommendationDossier rows to DB."""

    def test_persists_one_dossier_per_target(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        asyncio.run(builder.build(H_with_trader_outputs, db))
        assert db.add.call_count == len(H_with_trader_outputs.market_state.universe)

    def test_persists_commit_called(self, H_with_trader_outputs):
        builder = DossierBuilder()
        db = MagicMock()
        asyncio.run(builder.build(H_with_trader_outputs, db))
        db.commit.assert_called()


class TestDossierBuilderPreservesConvictionFormula:
    """Ensure the existing compute_conviction formula is preserved."""

    def test_compute_conviction_unchanged(self):
        c = compute_conviction(sharpe=2.0, mean_ic=0.05)
        assert abs(c - 0.6) < 1e-6

    def test_crisis_haircut_unchanged(self):
        base = compute_conviction(sharpe=2.0, mean_ic=0.05)
        crisis = compute_conviction(sharpe=2.0, mean_ic=0.05, crisis=True)
        assert abs(crisis / base - 0.7) < 1e-6
