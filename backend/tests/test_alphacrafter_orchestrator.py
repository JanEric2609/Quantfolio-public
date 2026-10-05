"""End-to-end acceptance test for the AlphaCrafter orchestrator pipeline.

Proves the acceptance criterion: the full Miner→Screener→Trader→Dossier
pipeline produces a persisted RecommendationDossier from real IC calculations
and a real vectorbt backtest, entirely within an in-memory SQLite database
(no network calls).
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    AlphacrafterJobRun,
    AppSetting,
    FactorsLibrary,
    Holding,
    Portfolio,
    RecommendationDossier,
    ScreenerRun,
    TraderBacktest,
    User,
)
from app.lab.alphacrafter.orchestrator import run_daily_alphacrafter, run_pipeline
from app.lab.alphacrafter.shared_memory import SharedMemoryH


# ---------------------------------------------------------------------------
# Test DB helpers (copied from test_alphacrafter_trader.py)
# ---------------------------------------------------------------------------


from conftest import _memory_db


def _seed_trend_bars(db, n_symbols: int = 5, days: int = 120):
    """Seed bars with DISTINCT linear trends per symbol so cross-sectional
    ranking is non-trivial.  Bars are anchored near *now* so build_panel's
    date-window query returns them."""
    symbols = [f"SYM{i}" for i in range(n_symbols)]
    base = datetime.now(UTC) - timedelta(days=days + 2)
    rng = np.random.default_rng(42)
    for i, sym in enumerate(symbols):
        # drift spans 0.001 to 0.005 — clearly different across symbols.
        drift = 0.001 * (i + 1)
        price = 100.0 * (1 + i * 0.1)  # different starting price too
        for d in range(days):
            price *= 1 + drift + rng.normal(0, 0.0002)
            ts = base + timedelta(days=d)
            db.execute(
                text(
                    """
                    INSERT INTO bar_prices
                      (symbol, ts, open, high, low, close, volume, currency, provider)
                    VALUES (:s, :ts, :p, :p, :p, :p, :v, 'USD', 'test')
                    """
                ),
                {"s": sym, "ts": ts, "p": float(price), "v": float(rng.uniform(1e6, 9e6))},
            )
    db.commit()
    return symbols


# ---------------------------------------------------------------------------
# Stub registry that returns empty fundamentals — no network calls.
# ---------------------------------------------------------------------------


class _NoopRegistry:
    """Returns empty fundamentals for all symbols — no provider calls made."""

    def get_fundamentals(self, symbol: str) -> dict:  # noqa: ARG002
        return {}


_NOOP_REGISTRY = _NoopRegistry()


@pytest.fixture(autouse=True)
def _stub_new_panel_pit_sources():
    """build_panel now calls three new PIT-join helpers (WRDS factor
    characteristics, IBES estimates, insider trading) once per symbol. Their
    read_* loaders fall back to a brand-new real DB session per call whenever
    QUANTFOLIO_PARQUET_PANEL_DIR is unset (see
    foundation/data_engineering/paths.py:get_panel_dir) -- on a machine with
    a locally loaded parquet panel and a real (possibly slow/unreachable)
    DATABASE_URL, that turned this file's SYM0..SYM4 fixtures into
    multi-hour hangs, since the IBES/insider ticker-match joins have no
    security_master gate to fail fast on for nonexistent symbols. None of
    these orchestrator tests exercise that data path, so stub it out.
    """
    with (
        patch("app.lab.alphacrafter.panel.pit_wrds_factor_characteristics_for_symbol", return_value=None),
        patch("app.lab.alphacrafter.panel.pit_ibes_estimate_signal_for_symbol", return_value=None),
        patch("app.lab.alphacrafter.panel.pit_insider_signal_for_symbol", return_value=None),
    ):
        yield


# ---------------------------------------------------------------------------
# Acceptance test
# ---------------------------------------------------------------------------


class TestOrchestratorEndToEnd:
    """Full Miner→Screener→Trader→Dossier pipeline acceptance test.

    This is intentionally slow — vectorbt import + real backtest sweep.
    Allow 4–5 minutes.
    """

    def test_full_pipeline_produces_persisted_dossier(self):
        """The complete pipeline runs end-to-end and persists exactly one
        RecommendationDossier with valid conviction and expected JSON keys."""
        db = _memory_db()

        # Step 2: Insert AppSetting rows to relax the Miner's validity gate.
        # With synthetic data the IC/ICIR thresholds must be zero to guarantee
        # survivors pass through to the Screener/Trader/Dossier stages.
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.commit()

        # Step 4: Seed bars for the universe (~120 days, 5 symbols).
        universe = _seed_trend_bars(db, n_symbols=5, days=120)

        # Step 3 + 5: Patch provider registry to avoid network, then run.
        with patch(
            "app.foundation.providers.registry.build_provider_registry",
            return_value=_NOOP_REGISTRY,
        ):
            result = asyncio.run(run_daily_alphacrafter(db, universe))

        # Step 6: High-level result assertions.
        if "error" in result:
            raise AssertionError(
                f"run_daily_alphacrafter returned an error: {result['error']}\n"
                f"Full result: {result}"
            )

        assert result["factors_generated"] >= 1, (
            f"Expected at least 1 factor generated, got {result['factors_generated']}"
        )
        assert result["factors_selected"] >= 1, (
            f"Expected at least 1 factor selected, got {result['factors_selected']}"
        )
        assert result["dossiers_created"] == 1, (
            f"Expected exactly 1 dossier created, got {result['dossiers_created']}"
        )

        # Step 7: Query the persisted dossier and validate its content.
        rows = db.execute(select(RecommendationDossier)).scalars().all()
        assert len(rows) == 1, f"Expected exactly 1 RecommendationDossier row, got {len(rows)}"

        row = rows[0]
        assert 0.0 <= row.conviction <= 0.95, (
            f"conviction {row.conviction} out of range [0.0, 0.95]"
        )

        dossier_dict = json.loads(row.dossier_json)
        assert isinstance(dossier_dict, dict), "dossier_json must deserialise to a dict"

        required_keys = {"conviction", "rationale", "risk_notes", "miner_factors", "sharpe_ratio"}
        missing = required_keys - dossier_dict.keys()
        assert not missing, (
            f"dossier_json missing required keys: {missing}\n"
            f"Present keys: {set(dossier_dict.keys())}"
        )


def _seed_portfolio_with_holdings(db, universe_symbols: list[str]) -> None:
    """Create a user, portfolio, and holdings for the given universe symbols."""
    user = User(
        id=_uuid.uuid4().hex,
        username=f"test_user_{_uuid.uuid4().hex[:8]}",
        password_hash="x",
    )
    db.add(user)
    db.flush()

    portfolio = Portfolio(
        id=_uuid.uuid4().hex,
        user_id=user.id,
        name="Test Portfolio",
    )
    db.add(portfolio)
    db.flush()

    for sym in universe_symbols:
        holding = Holding(
            id=_uuid.uuid4().hex,
            portfolio_id=portfolio.id,
            ticker=sym,
            name=f"{sym} Holding",
            asset_type="stock",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("100"),
        )
        db.add(holding)
    db.commit()


class TestPortfolioDossiers:
    """Pipeline generates dossiers for portfolio holdings + basket anchor."""

    def test_pipeline_creates_dossiers_for_holdings(self):
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.commit()

        universe = _seed_trend_bars(db, n_symbols=5, days=120)
        # Make 3 of the 5 symbols portfolio holdings.
        holding_symbols = universe[1:4]
        _seed_portfolio_with_holdings(db, holding_symbols)

        with patch(
            "app.foundation.providers.registry.build_provider_registry",
            return_value=_NOOP_REGISTRY,
        ):
            result = asyncio.run(run_daily_alphacrafter(db, universe))

        if "error" in result:
            raise AssertionError(f"pipeline error: {result['error']}")

        # Expect 1 (basket anchor universe[0]) + 3 (holdings) = 4 dossiers
        assert result["dossiers_created"] == 1 + len(holding_symbols), (
            f"Expected {1 + len(holding_symbols)} dossiers, got {result['dossiers_created']}"
        )

        # All expected symbols appear in dossier_symbols.
        expected_symbols = {universe[0]} | set(holding_symbols)
        actual_symbols = set(result.get("dossier_symbols", []))
        assert expected_symbols == actual_symbols, (
            f"Expected dossiers for {expected_symbols}, got {actual_symbols}"
        )


class TestAlphacrafterJobRun:
    """Async job lifecycle through AlphacrafterJobRun rows."""

    def test_job_creates_and_completes(self):
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.commit()

        universe = _seed_trend_bars(db, n_symbols=5, days=120)

        # Create a job run row (as the API endpoint does).
        job_run_id = _uuid.uuid4().hex
        job_run = AlphacrafterJobRun(
            id=job_run_id,
            status="running",
            progress_json=json.dumps({"stages": {}}),
        )
        db.add(job_run)
        db.commit()

        with patch(
            "app.foundation.providers.registry.build_provider_registry",
            return_value=_NOOP_REGISTRY,
        ):
            result = asyncio.run(run_daily_alphacrafter(db, universe, job_run_id=job_run_id))

        if "error" in result:
            raise AssertionError(f"pipeline error: {result['error']}")

        # Reload the job row and verify it was updated.
        db.expire_all()
        updated = db.get(AlphacrafterJobRun, job_run_id)
        assert updated is not None
        assert updated.status == "completed"
        assert updated.completed_at is not None

        # Verify progress_json contains stage entries.
        progress = json.loads(updated.progress_json) if updated.progress_json else {}
        assert "stages" in progress, f"progress missing 'stages': {progress}"
        for stage_name in ("miner", "screener", "trader", "dossier"):
            assert stage_name in progress["stages"], (
                f"Missing stage '{stage_name}' in progress.stages: {list(progress['stages'].keys())}"
            )

        # Verify each stage has a status.
        for stage_name, details in progress["stages"].items():
            assert "status" in details, f"Stage '{stage_name}' missing status: {details}"


# ---------------------------------------------------------------------------
# run_pipeline() — SharedMemoryH-based orchestrator tests
# ---------------------------------------------------------------------------


class TestRunPipelineSharedMemory:
    """Tests for run_pipeline() which uses SharedMemoryH with checkpointing."""

    def test_pipeline_creates_shared_memory_h_and_persists(self):
        """run_pipeline creates a SharedMemoryH, runs all agents, and persists H
        to the AlphacrafterJobRun at the end."""
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.commit()

        universe = _seed_trend_bars(db, n_symbols=5, days=120)

        job_run_id = _uuid.uuid4().hex
        job_run = AlphacrafterJobRun(
            id=job_run_id,
            status="running",
            progress_json=json.dumps({}),
        )
        db.add(job_run)
        db.commit()

        with patch(
            "app.foundation.providers.registry.build_provider_registry",
            return_value=_NOOP_REGISTRY,
        ):
            result = asyncio.run(
                run_pipeline(db, universe, job_run_id=job_run_id)
            )

        assert "factors_generated" in result, f"Missing 'factors_generated' in result: {result}"
        assert "factors_selected" in result, f"Missing 'factors_selected' in result: {result}"
        assert "dossiers_created" in result, f"Missing 'dossiers_created' in result: {result}"
        assert "shared_memory" in result, f"Missing 'shared_memory' in result: {result}"

        h_dict = result["shared_memory"]
        assert isinstance(h_dict, dict), f"shared_memory should be dict, got {type(h_dict)}"
        assert "market_state" in h_dict, f"shared_memory missing 'market_state': {h_dict}"
        assert "factor_states" in h_dict, f"shared_memory missing 'factor_states': {h_dict}"

        db.expire_all()
        updated = db.get(AlphacrafterJobRun, job_run_id)
        assert updated is not None
        assert updated.status == "completed"
        assert updated.completed_at is not None

    def test_pipeline_checkpointing_h_persisted_after_each_agent(self):
        """run_pipeline persists SharedMemoryH after each agent stage
        (miner, screener, trader, dossier) — not just at the end."""
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.commit()

        universe = _seed_trend_bars(db, n_symbols=5, days=120)

        job_run_id = _uuid.uuid4().hex
        job_run = AlphacrafterJobRun(
            id=job_run_id,
            status="running",
            progress_json=json.dumps({}),
        )
        db.add(job_run)
        db.commit()

        persist_call_count = 0
        original_persist = SharedMemoryH.persist

        def counting_persist(self_h, db_session, jid=None):
            nonlocal persist_call_count
            persist_call_count += 1
            return original_persist(self_h, db_session, jid)

        with (
            patch(
                "app.foundation.providers.registry.build_provider_registry",
                return_value=_NOOP_REGISTRY,
            ),
            patch.object(
                SharedMemoryH, "persist", counting_persist,
            ),
        ):
            result = asyncio.run(
                run_pipeline(db, universe, job_run_id=job_run_id)
            )

        if "error" in result:
            raise AssertionError(f"pipeline error: {result['error']}")

        assert persist_call_count >= 4, (
            f"Expected at least 4 persist calls (after each agent + final), "
            f"got {persist_call_count}"
        )

    def test_pipeline_return_dict_fields(self):
        """run_pipeline returns a dict with factors_generated, factors_selected,
        dossiers_created, and shared_memory."""
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.commit()

        universe = _seed_trend_bars(db, n_symbols=5, days=120)

        with patch(
            "app.foundation.providers.registry.build_provider_registry",
            return_value=_NOOP_REGISTRY,
        ):
            result = asyncio.run(run_pipeline(db, universe))

        required_keys = {"factors_generated", "factors_selected", "dossiers_created", "shared_memory"}
        missing = required_keys - set(result.keys())
        assert not missing, f"run_pipeline result missing keys: {missing}"

        assert isinstance(result["factors_generated"], int)
        assert isinstance(result["factors_selected"], int)
        assert isinstance(result["dossiers_created"], int)
        assert result["factors_generated"] >= 0
        assert result["factors_selected"] >= 0
        assert result["dossiers_created"] >= 0


# ---------------------------------------------------------------------------
# audit-fixes-2026-08 todo 19: settings-driven Stage-3 sweep + propose_llm
# ---------------------------------------------------------------------------


def _synthetic_panel(n_symbols: int = 6, n_days: int = 300, seed: int = 11):
    """Deterministic trending panel — no DB bars or providers needed."""
    import pandas as pd

    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n_days)
    cols = [f"SYM{i}" for i in range(n_symbols)]
    drifts = np.linspace(0.0005, 0.004, n_symbols)
    steps = rng.normal(0.0, 0.01, size=(n_days, n_symbols)) + drifts
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=idx, columns=cols)
    volume = pd.DataFrame(rng.uniform(1e6, 9e6, size=(n_days, n_symbols)), index=idx, columns=cols)
    return {"close": close, "volume": volume}


class _StubBacktest:
    def __init__(self):
        self.sharpe_ratio = 1.0
        self.max_drawdown = -0.1
        self.total_return = 0.2
        self.annual_return = 0.2
        self.final_equity = 1200.0
        self.error = None


def _fast_pipeline_patches(panel):
    """Patch panel builders + vectorbt backtest + dossier builder for speed."""
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch as _patch

    stack = ExitStack()

    async def fake_run_backtest(spec, prices_df):  # noqa: ARG001
        return _StubBacktest()

    async def fake_build_dossier(*args, **kwargs):  # noqa: ARG001
        return None

    def noop_registry(db=None):  # noqa: ARG001
        return _NOOP_REGISTRY

    stack.enter_context(_patch("app.lab.alphacrafter.miner.build_panel", return_value=panel))
    stack.enter_context(_patch("app.lab.alphacrafter.trader.build_panel", return_value=panel))
    stack.enter_context(_patch("app.lab.alphacrafter.trader.run_backtest", fake_run_backtest))
    stack.enter_context(_patch("app.lab.alphacrafter.orchestrator.build_dossier", fake_build_dossier))
    stack.enter_context(_patch("app.foundation.providers.registry.build_provider_registry", noop_registry))
    return stack


class TestStage3SettingsDrivenSweep:
    """Orchestrator Stage 3 must sweep the settings-driven grid (todo 19a)."""

    def test_commission_setting_flows_into_trader_backtest_specs(self):
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_commissions", value_json='"0.002"'))
        db.commit()

        universe = [f"SYM{i}" for i in range(6)]
        panel = _synthetic_panel(n_symbols=len(universe))

        with _fast_pipeline_patches(panel):
            result = asyncio.run(run_daily_alphacrafter(db, universe))

        if "error" in result:
            raise AssertionError(f"pipeline error: {result['error']}")

        rows = db.execute(select(TraderBacktest)).scalars().all()
        assert rows, "Expected TraderBacktest rows from the Stage-3 sweep"
        assert any("0.002" in row.spec_json for row in rows), (
            "Settings commission 0.002 must appear in persisted spec_json; "
            f"got specs: {[r.spec_json for r in rows]}"
        )

    def test_sweep_cap_truncates_with_warning(self, caplog):
        """A grid larger than the 12-config cap is truncated loudly, never silently."""
        import logging as _logging

        from app.lab.alphacrafter.trader import run_trader

        db = _memory_db()
        universe = [f"SYM{i}" for i in range(6)]
        panel = _synthetic_panel(n_symbols=len(universe))

        # 3 freqs × 3 sizes × 2 commissions = 18 configs > cap of 12.
        db.add(AppSetting(key="alphacrafter_rebalance_freqs", value_json='"D,W,M"'))
        db.add(AppSetting(key="alphacrafter_position_sizes", value_json='"0.05,0.1,0.2"'))
        db.add(AppSetting(key="alphacrafter_commissions", value_json='"0.001,0.002"'))
        db.commit()

        factors = [
            FactorsLibrary(
                name=f"f{i}",
                formula_json=json.dumps({"formula": "rank(close)", "dsl": "rank(close)", "name": f"f{i}"}),
                source="test",
                ic_summary_json=json.dumps({"ic": 0.05, "icir": 1.0, "ic_series": [0.05] * 10}),
            )
            for i in range(2)
        ]
        db.add_all(factors)
        db.flush()
        screener_run = ScreenerRun(
            regime_label=None,
            selected_factor_ids=",".join(f.id for f in factors),
            scores_json="{}",
        )
        db.add(screener_run)
        db.commit()

        now = datetime.now(UTC)
        with _fast_pipeline_patches(panel), caplog.at_level(_logging.WARNING, logger="app.lab.alphacrafter.trader"):
            results = asyncio.run(
                run_trader(
                    db, screener_run.id, universe,
                    now - timedelta(days=300), now,
                    num_configs=3, registry=_NOOP_REGISTRY,
                    max_sweep_configs=12,
                )
            )

        assert len(results) <= 12
        rows = db.execute(select(TraderBacktest)).scalars().all()
        assert len(rows) <= 12, f"Sweep cap violated: {len(rows)} TraderBacktest rows"
        assert "truncating" in caplog.text, (
            f"Expected a warn-truncate log when exceeding the cap; got: {caplog.records}"
        )


class TestAsyncPipelineProposeLlm:
    """run_pipeline must plumb alphacrafter_propose_llm into the miner (todo 19b)."""

    def test_propose_llm_true_yields_extra_factors_in_memory_stage(self):
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_propose_llm", value_json="true"))
        db.commit()

        universe = [f"SYM{i}" for i in range(6)]
        panel = _synthetic_panel(n_symbols=len(universe))

        captured: dict = {}

        async def fake_extra_factors(db_session, panel_arg, regime_label, settings):  # noqa: ARG001
            captured["called"] = True
            captured["regime_label"] = regime_label
            return [{
                "name": "stub_llm_factor",
                "formula": "rank(close)",
                "source": "llm",
                "dsl": "rank(close)",
            }]

        with _fast_pipeline_patches(panel), patch(
            "app.lab.alphacrafter.miner._llm_extra_factors",
            fake_extra_factors,
        ):
            result = asyncio.run(run_pipeline(db, universe))

        assert captured.get("called") is True, (
            "propose_llm=true must reach the miner's LLM proposal branch on the async path"
        )
        memory_dump = json.dumps(result["shared_memory"])
        assert "stub_llm_factor" in memory_dump, (
            "Stubbed LLM proposal factor must appear in SharedMemoryH factor_states"
        )

    def test_propose_llm_false_skips_proposer(self):
        db = _memory_db()
        db.add(AppSetting(key="alphacrafter_min_ic", value_json="0.0"))
        db.add(AppSetting(key="alphacrafter_min_icir", value_json="0.0"))
        db.commit()

        universe = [f"SYM{i}" for i in range(6)]
        panel = _synthetic_panel(n_symbols=len(universe))

        captured: dict = {}

        async def fake_extra_factors(*args, **kwargs):  # noqa: ARG001
            captured["called"] = True
            return []

        with _fast_pipeline_patches(panel), patch(
            "app.lab.alphacrafter.miner._llm_extra_factors",
            fake_extra_factors,
        ):
            asyncio.run(run_pipeline(db, universe))

        assert captured.get("called") is None, (
            "Proposer must NOT be called when alphacrafter_propose_llm is unset/false"
        )
