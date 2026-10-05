"""Integration tests for the AlphaCrafter SharedMemoryH pipeline.

These tests verify the full pipeline flow with mocked agents, ensuring:
1. SharedMemoryH is created with all sections populated
2. H is persisted after each agent (checkpointing)
3. Return dict has required fields
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import AlphacrafterJobRun
from app.lab.alphacrafter.data_ingestion import DataAvailability
from app.lab.alphacrafter.orchestrator import run_pipeline
from app.lab.alphacrafter.shared_memory import SharedMemoryH, FactorState, FactorMetrics


# ---------------------------------------------------------------------------
# Test DB helpers
# ---------------------------------------------------------------------------


def _memory_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS bar_prices (
                    symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,
                    close REAL, volume REAL, currency TEXT, provider TEXT
                )
                """
            )
        )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ---------------------------------------------------------------------------
# Mock data factories
# ---------------------------------------------------------------------------


@dataclass
class MockFactorCandidate:
    """Minimal FactorCandidate for testing."""

    name: str
    formula: str
    source: str = "seed"
    ic: float = 0.05
    icir: float = 0.5
    turnover: float = 0.1
    decay_halflife_days: float | None = 30.0
    valid: bool = True
    validation_notes: str = ""
    dsl: str | None = None
    ic_series: list[float] = field(default_factory=list)
    id: str | None = None


@dataclass
class MockTraderConfig:
    """Minimal TraderConfig for testing."""

    rebalance_freq: str = "W"
    position_size: float = 0.1
    commission: float = 0.001
    max_positions: int = 5


@dataclass
class MockTraderResult:
    """Minimal TraderResult for testing."""

    config: MockTraderConfig
    sharpe_ratio: float = 1.5
    max_drawdown: float = -0.1
    total_return: float = 0.2


@dataclass
class MockScreenerResult:
    """Minimal screener result for testing."""

    screener_run_id: str = "test_run"
    regime_label: str | None = "bull"
    selected_factor_ids: list[str] = field(default_factory=list)
    regime_score: float = 0.8
    crisis: bool = False


def _make_factor_states(candidates: list) -> list[FactorState]:
    """Convert MockFactorCandidates into FactorState objects for H."""
    return [
        FactorState(
            id=c.id or f"factor_{i}",
            name=c.name,
            source=c.source,
            category="unknown",
            formula=c.formula,
            dsl=None,
            metrics=FactorMetrics(
                ic=c.ic, icir=c.icir, turnover=0.1,
                decay_halflife_days=30.0, n_obs=100,
            ),
            regime_applicability={},
        )
        for i, c in enumerate(candidates)
    ]


def _make_candidates(n: int = 3) -> list[MockFactorCandidate]:
    """Create n mock factor candidates with valid=True and ids set."""
    return [
        MockFactorCandidate(
            name=f"factor_{i}",
            formula="close / shift(close, 5) - 1",
            id=_uuid.uuid4().hex,
            ic=0.03 + i * 0.01,
            icir=0.4 + i * 0.1,
        )
        for i in range(n)
    ]


def _make_data_availability() -> DataAvailability:
    """Create a mock DataAvailability with some symbols."""
    return DataAvailability(price_symbols=5, fundamental_symbols=3)


def _make_trader_results() -> list[MockTraderResult]:
    """Create mock trader results."""
    return [
        MockTraderResult(
            config=MockTraderConfig(),
            sharpe_ratio=1.5,
            max_drawdown=-0.1,
            total_return=0.2,
        )
    ]


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestPipelineCreatesPopulatedH:
    """Test that the full pipeline creates a populated SharedMemoryH."""

    def test_pipeline_creates_populated_h(self):
        """Mock all agents and verify H is created with all sections populated."""
        db = _memory_db()
        db.commit()

        universe = ["AAPL", "MSFT", "GOOGL"]
        candidates = _make_candidates(3)
        factor_states = _make_factor_states(candidates)
        selected_ids = [c.id for c in candidates[:2]]

        def mock_miner_run(self, h, db_, propose_llm=False):
            h.factor_states = factor_states
            h.miner_outputs = {"n_evaluated": 3, "n_valid": 3, "n_persisted": 3}
            return h

        def mock_screener_run(self, h, db_):
            h.screener_outputs = {
                "selected_factor_ids": selected_ids,
                "regime_label": "bull",
                "crisis": False,
                "regime_score": 0.8,
                "rejected": [],
            }
            h.regime_assessment = {"regime_label": "bull", "crisis": False, "regime_score": 0.8}
            return h

        async def mock_trader_run(self, h, db_):
            h.trader_outputs = {
                "n_configs_run": 1,
                "n_factors": 2,
                "configs": [{"rebalance_freq": "W", "position_size": 0.1, "commission": 0.001,
                             "sharpe_ratio": 1.5, "max_drawdown": -0.1, "total_return": 0.2, "rank": 1}],
            }
            return h

        async def mock_dossier_build(self, h, db_, user_id=None):
            h.evaluation = {"dossiers_created": 1, "conviction_scores": {"AAPL": 0.8}}
            return h

        with (
            patch(
                "app.lab.alphacrafter.data_ingestion.AlphaCrafterDataIngestion.ensure_data",
                new_callable=AsyncMock,
                return_value=_make_data_availability(),
            ),
            patch("app.lab.alphacrafter.miner.MinerAgent.run", mock_miner_run),
            patch("app.lab.alphacrafter.screener.ScreenerAgent.run", mock_screener_run),
            patch("app.lab.alphacrafter.trader.TraderAgent.run", mock_trader_run),
            patch("app.lab.alphacrafter.dossier.DossierBuilder.build", mock_dossier_build),
        ):
            result = asyncio.run(run_pipeline(db, universe))

        # Verify return dict has required fields
        assert "factors_generated" in result
        assert "factors_selected" in result
        assert "dossiers_created" in result
        assert "shared_memory" in result

        assert result["factors_generated"] == 3
        assert result["factors_selected"] == 2
        assert result["dossiers_created"] == 1

        h_dict = result["shared_memory"]
        assert isinstance(h_dict, dict)

        # Verify H has all sections populated
        assert "market_state" in h_dict
        assert "factor_states" in h_dict
        assert "miner_outputs" in h_dict
        assert "screener_outputs" in h_dict
        assert "trader_outputs" in h_dict
        assert "evaluation" in h_dict
        assert "agent_history" in h_dict

        ms = h_dict["market_state"]
        assert ms["universe"] == universe
        assert "regime_label" in ms

        assert len(h_dict["factor_states"]) == 3
        assert h_dict["factor_states"][0]["name"] == "factor_0"

        assert h_dict["miner_outputs"]["n_valid"] == 3
        assert len(h_dict["screener_outputs"]["selected_factor_ids"]) == 2

        agents_in_history = [h["agent"] for h in h_dict["agent_history"]]
        assert "orchestrator" in agents_in_history
        assert "miner" in agents_in_history
        assert "screener" in agents_in_history
        assert "trader" in agents_in_history
        assert "dossier" in agents_in_history

    def test_pipeline_return_dict_required_fields(self):
        """Verify the return dict has all required fields with correct types."""
        db = _memory_db()
        db.commit()

        def mock_miner_run(self, h, db_, propose_llm=False):
            return h

        def mock_screener_run(self, h, db_):
            return h

        async def mock_trader_run(self, h, db_):
            return h

        async def mock_dossier_build(self, h, db_, user_id=None):
            h.evaluation = {"dossiers_created": 0, "conviction_scores": {}}
            return h

        with (
            patch(
                "app.lab.alphacrafter.data_ingestion.AlphaCrafterDataIngestion.ensure_data",
                new_callable=AsyncMock,
                return_value=_make_data_availability(),
            ),
            patch("app.lab.alphacrafter.miner.MinerAgent.run", mock_miner_run),
            patch("app.lab.alphacrafter.screener.ScreenerAgent.run", mock_screener_run),
            patch("app.lab.alphacrafter.trader.TraderAgent.run", mock_trader_run),
            patch("app.lab.alphacrafter.dossier.DossierBuilder.build", mock_dossier_build),
        ):
            result = asyncio.run(run_pipeline(db, ["AAPL"]))

        required_keys = {"factors_generated", "factors_selected", "dossiers_created", "shared_memory"}
        missing = required_keys - set(result.keys())
        assert not missing, f"Missing keys: {missing}"

        assert isinstance(result["factors_generated"], int)
        assert isinstance(result["factors_selected"], int)
        assert isinstance(result["dossiers_created"], int)
        assert isinstance(result["shared_memory"], dict)


class TestPipelineCheckpointing:
    """Test that H is persisted after each agent."""

    def test_h_persisted_after_each_agent(self):
        """Verify H is persisted after each agent (miner, screener, trader, dossier)."""
        db = _memory_db()
        db.commit()

        universe = ["AAPL", "MSFT", "GOOGL"]
        job_run_id = _uuid.uuid4().hex
        job_run = AlphacrafterJobRun(
            id=job_run_id,
            status="running",
            progress_json=json.dumps({}),
        )
        db.add(job_run)
        db.commit()

        candidates = _make_candidates(3)
        factor_states = _make_factor_states(candidates)
        selected_ids = [c.id for c in candidates[:2]]

        def mock_miner_run(self, h, db_, propose_llm=False):
            h.factor_states = factor_states
            h.miner_outputs = {"n_evaluated": 3, "n_valid": 3, "n_persisted": 3}
            return h

        def mock_screener_run(self, h, db_):
            h.screener_outputs = {
                "selected_factor_ids": selected_ids,
                "regime_label": "bull",
                "crisis": False,
                "regime_score": 0.8,
                "rejected": [],
            }
            h.regime_assessment = {"regime_label": "bull", "crisis": False}
            return h

        async def mock_trader_run(self, h, db_):
            h.trader_outputs = {
                "n_configs_run": 1,
                "n_factors": 2,
                "configs": [{"rebalance_freq": "W", "position_size": 0.1, "commission": 0.001,
                             "sharpe_ratio": 1.5, "max_drawdown": -0.1, "total_return": 0.2, "rank": 1}],
            }
            return h

        async def mock_dossier_build(self, h, db_, user_id=None):
            h.evaluation = {"dossiers_created": 1, "conviction_scores": {"AAPL": 0.8}}
            return h

        persist_call_count = 0
        original_persist = SharedMemoryH.persist

        def counting_persist(self_h, db_session, jid=None):
            nonlocal persist_call_count
            persist_call_count += 1
            return original_persist(self_h, db_session, jid)

        with (
            patch(
                "app.lab.alphacrafter.data_ingestion.AlphaCrafterDataIngestion.ensure_data",
                new_callable=AsyncMock,
                return_value=_make_data_availability(),
            ),
            patch("app.lab.alphacrafter.miner.MinerAgent.run", mock_miner_run),
            patch("app.lab.alphacrafter.screener.ScreenerAgent.run", mock_screener_run),
            patch("app.lab.alphacrafter.trader.TraderAgent.run", mock_trader_run),
            patch("app.lab.alphacrafter.dossier.DossierBuilder.build", mock_dossier_build),
            patch.object(SharedMemoryH, "persist", counting_persist),
        ):
            asyncio.run(run_pipeline(db, universe, job_run_id=job_run_id))

        assert persist_call_count >= 4, (
            f"Expected at least 4 persist calls (after each agent + final), got {persist_call_count}"
        )

        db.expire_all()
        updated = db.get(AlphacrafterJobRun, job_run_id)
        assert updated is not None
        assert updated.status == "completed"
        # persist() writes to the shared_memory column (not progress_json)
        assert updated.shared_memory is not None, "shared_memory column should be populated after pipeline"

    def test_persist_called_after_miner(self):
        """Verify H is persisted immediately after miner completes."""
        db = _memory_db()
        db.commit()

        job_run_id = _uuid.uuid4().hex
        job_run = AlphacrafterJobRun(
            id=job_run_id,
            status="running",
            progress_json=json.dumps({}),
        )
        db.add(job_run)
        db.commit()

        candidates = _make_candidates(2)
        factor_states = _make_factor_states(candidates)

        def mock_miner_run(self, h, db_, propose_llm=False):
            h.factor_states = factor_states
            h.miner_outputs = {"n_evaluated": 2, "n_valid": 2, "n_persisted": 2}
            return h

        def mock_screener_run(self, h, db_):
            return h

        async def mock_trader_run(self, h, db_):
            return h

        async def mock_dossier_build(self, h, db_, user_id=None):
            h.evaluation = {"dossiers_created": 0, "conviction_scores": {}}
            return h

        persist_call_args = []
        original_persist = SharedMemoryH.persist

        def tracking_persist(self_h, db_session, jid=None):
            persist_call_args.append({
                "factor_count": len(self_h.factor_states),
                "has_miner_outputs": bool(self_h.miner_outputs),
            })
            return original_persist(self_h, db_session, jid)

        with (
            patch(
                "app.lab.alphacrafter.data_ingestion.AlphaCrafterDataIngestion.ensure_data",
                new_callable=AsyncMock,
                return_value=_make_data_availability(),
            ),
            patch("app.lab.alphacrafter.miner.MinerAgent.run", mock_miner_run),
            patch("app.lab.alphacrafter.screener.ScreenerAgent.run", mock_screener_run),
            patch("app.lab.alphacrafter.trader.TraderAgent.run", mock_trader_run),
            patch("app.lab.alphacrafter.dossier.DossierBuilder.build", mock_dossier_build),
            patch.object(SharedMemoryH, "persist", tracking_persist),
        ):
            asyncio.run(run_pipeline(db, ["AAPL"], job_run_id=job_run_id))

        # First persist call (after miner) should have factor_states populated
        assert len(persist_call_args) >= 2, "Expected at least 2 persist calls"
        assert persist_call_args[0]["factor_count"] == 2, "First persist should have 2 factors"
        assert persist_call_args[0]["has_miner_outputs"] is True, "First persist should have miner_outputs"
