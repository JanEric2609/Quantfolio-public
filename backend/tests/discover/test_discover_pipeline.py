"""Comprehensive tests for the Phase 4 Discover pipeline.

Covers the new headless components introduced in Phase 4:
  - MacroRegimeGate (regime-aware discovery gating)
  - DegradationAssessor (provider fallback scoring)
  - New pipeline stages (alpha_miner, alpha_screener, sentiment_fundamentals)
  - DossierWriter (LLM dossier generation with fundamentals/sentiment fields)
  - Composite scoring weights and regime-gate blocking behaviour

All tests use an in-memory SQLite database and mock every external network
call (providers, LLM, yfinance, finnhub, etc.). No asyncio, no real HTTP.
"""
from __future__ import annotations

import json
import math
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from conftest import _memory_db

from app.foundation.models.entities import DiscoverCandidate, DiscoverRun
from app.foundation.models.entities._core import uuid_pk
from app.decision.discover import (
    DegradationAssessor,
    DegradationResult,
    MacroRegimeGate,
    RegimeGateResult,
)
from app.decision.discover.dossier_writer import DossierOutputModel, write_dossier
from app.decision.discover.pipeline import (
    run_candidate_pipeline,
    stage_alpha_miner,
    stage_alpha_screener,
    stage_sentiment_fundamentals,
)


def _consume_pipeline(generator):
    """Consume a run_candidate_pipeline generator and return its StopIteration value."""
    try:
        while True:
            next(generator)
    except StopIteration as exc:
        return exc.value


# ======================================================================
# Import sanity
# ======================================================================


def test_discover_module_imports():
    """All discover module public imports resolve without side effects."""
    from app.decision.discover.dossier_writer import DossierOutputModel, write_dossier
    from app.decision.discover.pipeline import (
        run_candidate_pipeline,
        stage_alpha_miner,
        stage_alpha_screener,
        stage_sentiment_fundamentals,
    )

    assert MacroRegimeGate
    assert RegimeGateResult
    assert DegradationAssessor
    assert DegradationResult
    assert run_candidate_pipeline
    assert stage_alpha_miner
    assert stage_alpha_screener
    assert stage_sentiment_fundamentals
    assert DossierOutputModel
    assert write_dossier


# ======================================================================
# MacroRegimeGate
# ======================================================================


class TestMacroRegimeGate:
    def test_no_snapshot_falls_back_to_rule_based_regime(self):
        """ADR 0014 §2: no HMM regime_snapshots row falls back to the same
        rule-based snapshot RegimeChip reads, instead of a hardcoded
        'unknown'/0.5 sentinel — so dossier and chip agree during the gap."""
        db = _memory_db()
        with (
            patch(
                "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
                return_value=None,
            ),
            patch(
                "app.lab.regime.macro_snapshot.get_or_refresh_regime",
                return_value={"label": "low_vol", "confidence": 0.6, "vix": 18.0},
            ),
        ):
            gate = MacroRegimeGate(db)
            result = gate.assess("AAPL")

        assert result.regime_label == "low_vol"
        assert result.risk_on is False
        assert result.crisis is False
        assert result.allowed is True
        assert result.confidence == pytest.approx(0.6)
        assert result.reason is None

    def test_no_snapshot_fallback_blocks_on_bear(self):
        db = _memory_db()
        with (
            patch(
                "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
                return_value=None,
            ),
            patch(
                "app.lab.regime.macro_snapshot.get_or_refresh_regime",
                return_value={"label": "bear", "confidence": 0.8, "vix": 25.0},
            ),
        ):
            gate = MacroRegimeGate(db)
            result = gate.assess("AAPL")

        assert result.regime_label == "bear"
        assert result.crisis is True
        assert result.allowed is False

    def test_bull_allowed(self):
        db = _memory_db()
        with patch(
            "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
            return_value={"label": "bull", "score": 0.92},
        ):
            gate = MacroRegimeGate(db)
            result = gate.assess("AAPL")

        assert result.regime_label == "bull"
        assert result.risk_on is True
        assert result.crisis is False
        assert result.allowed is True
        assert result.confidence == pytest.approx(0.92)
        assert result.reason is None

    def test_bear_blocked(self):
        db = _memory_db()
        with patch(
            "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
            return_value={"label": "bear", "score": 0.71},
        ):
            gate = MacroRegimeGate(db)
            result = gate.assess("AAPL")

        assert result.regime_label == "bear"
        assert result.risk_on is False
        assert result.crisis is True
        assert result.allowed is False
        assert "bear" in (result.reason or "").lower()

    def test_crisis_blocked_by_vix(self):
        db = _memory_db()
        with patch(
            "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
            return_value={"label": "bull", "score": 0.85, "vix": 35.0},
        ):
            gate = MacroRegimeGate(db)
            result = gate.assess("AAPL")

        assert result.regime_label == "bull"
        assert result.crisis is True
        assert result.allowed is False
        assert "crisis" in (result.reason or "").lower()

    def test_block_all_factory(self):
        gate = MacroRegimeGate.block_all("manual test block")

        assert gate.blocks_discovery() is True
        ctx = gate.context_dict()
        assert ctx["regime_label"] == "blocked"
        assert ctx["allowed"] is False
        assert ctx["risk_on"] is False
        assert ctx["crisis"] is False
        assert ctx["confidence"] == 0.0
        assert ctx["reason"] == "manual test block"

    def test_context_dict(self):
        db = _memory_db()
        with patch(
            "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
            return_value={"label": "sideways", "score": 0.77, "source": "jump"},
        ):
            gate = MacroRegimeGate(db)
            ctx = gate.context_dict()

        assert ctx == {
            "regime_label": "sideways",
            "risk_on": False,
            "crisis": False,
            "confidence": pytest.approx(0.77),
            "allowed": True,
            "reason": None,
            "source": "jump",
        }

    def test_context_dict_without_a_jump_snapshot_reads_the_chip_state(self):
        """Without a jump snapshot the gate reads the same reader the header
        chip does, and names the jump model as the source."""
        db = _memory_db()
        with (
            patch("app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot", return_value=None),
            patch(
                "app.lab.regime.macro_snapshot.get_or_refresh_regime",
                return_value={"label": "bull", "confidence": 0.7, "vix": 15.0},
            ),
        ):
            ctx = MacroRegimeGate(db).context_dict()

        assert ctx["regime_label"] == "bull"
        assert ctx["source"] == "jump"

    def test_alignment_score(self):
        db = _memory_db()
        with patch(
            "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
            return_value={"label": "bull", "score": 0.9},
        ):
            bull_gate = MacroRegimeGate(db)
            assert bull_gate.alignment_score() == pytest.approx(1.0)

        with patch(
            "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
            return_value={"label": "sideways", "score": 0.8},
        ):
            sideways_gate = MacroRegimeGate(db)
            assert sideways_gate.alignment_score() == pytest.approx(0.7)

        with (
            patch(
                "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
                return_value=None,
            ),
            patch(
                "app.lab.regime.macro_snapshot.get_or_refresh_regime",
                return_value={"label": "unknown", "confidence": None, "vix": 18.0},
            ),
        ):
            unknown_gate = MacroRegimeGate(db)
            assert unknown_gate.alignment_score() == pytest.approx(0.6)

        blocked_gate = MacroRegimeGate.block_all("block")
        assert blocked_gate.alignment_score() == pytest.approx(0.0)


# ======================================================================
# DegradationAssessor
# ======================================================================


class _FakeProvider:
    def __init__(self, name: str):
        self.name = name
        self.enabled = True


class _FakeRegistry:
    def __init__(self, providers: list[_FakeProvider], quote_result: dict):
        self.providers = providers
        self._quote_result = quote_result

    def get_quote(self, symbol: str) -> dict:
        return self._quote_result


class TestDegradationAssessor:
    def test_degradation_result_dataclass_fields(self):
        result = DegradationResult(
            symbol="AAPL",
            degradation_score=0.25,
            provider_chain=["finnhub", "yfinance"],
            fallbacks_used=1,
            warnings=["Primary provider failed"],
        )
        assert result.symbol == "AAPL"
        assert result.degradation_score == pytest.approx(0.25)
        assert result.provider_chain == ["finnhub", "yfinance"]
        assert result.fallbacks_used == 1
        assert result.warnings == ["Primary provider failed"]

    def test_primary_provider_success_score_zero(self):
        db = _memory_db()
        registry = _FakeRegistry(
            providers=[_FakeProvider("finnhub")],
            quote_result={"ok": True, "provider": "finnhub", "data": {"close": 150.0}},
        )
        with patch(
            "app.decision.discover.provider_degradation.build_provider_registry",
            return_value=registry,
        ):
            result = DegradationAssessor().assess("AAPL", db)

        assert result.symbol == "AAPL"
        assert result.degradation_score == pytest.approx(0.0)
        assert result.provider_chain == ["finnhub"]
        assert result.fallbacks_used == 0
        assert result.warnings == []

    def test_fallback_success(self):
        db = _memory_db()
        registry = _FakeRegistry(
            providers=[_FakeProvider("finnhub"), _FakeProvider("yfinance")],
            quote_result={"ok": True, "provider": "yfinance", "data": {"close": 150.0}},
        )
        with patch(
            "app.decision.discover.provider_degradation.build_provider_registry",
            return_value=registry,
        ):
            result = DegradationAssessor().assess("AAPL", db)

        assert result.symbol == "AAPL"
        # With a 2-provider chain the code scales fallback_index / (len - 1).
        assert result.degradation_score == pytest.approx(1.0)
        assert result.provider_chain == ["finnhub", "yfinance"]
        assert result.fallbacks_used == 1
        assert any("fallback" in w for w in result.warnings)

    def test_all_providers_fail_score_one(self):
        db = _memory_db()
        registry = _FakeRegistry(
            providers=[_FakeProvider("finnhub"), _FakeProvider("yfinance")],
            quote_result={"ok": False, "provider": "registry", "error": "unavailable"},
        )
        with patch(
            "app.decision.discover.provider_degradation.build_provider_registry",
            return_value=registry,
        ):
            result = DegradationAssessor().assess("AAPL", db)

        assert result.symbol == "AAPL"
        assert result.degradation_score == pytest.approx(1.0)
        assert result.provider_chain == ["finnhub", "yfinance"]
        assert result.fallbacks_used == 2
        assert result.warnings

    def test_batch_assess(self):
        db = _memory_db()
        registry = _FakeRegistry(
            providers=[_FakeProvider("finnhub")],
            quote_result={"ok": True, "provider": "finnhub", "data": {"close": 100.0}},
        )
        with patch(
            "app.decision.discover.provider_degradation.build_provider_registry",
            return_value=registry,
        ):
            results = DegradationAssessor().assess_batch(["AAPL", "MSFT"], db)

        assert len(results) == 2
        assert results[0].symbol == "AAPL"
        assert results[1].symbol == "MSFT"
        assert all(r.degradation_score == pytest.approx(0.0) for r in results)


# ======================================================================
# Pipeline stages
# ======================================================================


class TestPipelineStages:
    def test_stage_alpha_miner_fail_open_when_alphacrafter_absent(self, monkeypatch):
        db = _memory_db()
        monkeypatch.setattr(
            "app.decision.discover.pipeline._ALPHACRAFTER_AVAILABLE", False
        )
        monkeypatch.setattr(
            "app.decision.discover.pipeline.alpha_miner_module", None
        )
        monkeypatch.setattr(
            "app.decision.discover.pipeline.alpha_build_panel", None
        )

        scores, reject = stage_alpha_miner(db, "AAPL")

        assert reject is None
        assert scores is not None
        assert scores["ic"] == pytest.approx(0.0)
        assert scores["icir"] == pytest.approx(0.0)
        assert scores["exposure_score"] is None
        assert scores["n_factors"] == 0
        assert "alphacrafter_miner_unavailable" in scores["concerns"]

    def test_stage_alpha_screener_regime_affinity(self, monkeypatch):
        db = _memory_db()
        # Seed a bull regime snapshot so the stage sees a known label.
        with patch(
            "app.decision.discover.regime_gate.RegimeStore.get_latest_snapshot",
            return_value={"label": "bull", "score": 0.9},
        ):
            fake_screener = MagicMock()
            fake_screener.regime_affinity = MagicMock(return_value=0.8)
            monkeypatch.setattr(
                "app.decision.discover.pipeline._ALPHACRAFTER_AVAILABLE", True
            )
            monkeypatch.setattr(
                "app.decision.discover.pipeline.alpha_screener_module", fake_screener
            )

            scores, reject = stage_alpha_screener(db, "AAPL")

        assert reject is None
        assert scores is not None
        assert scores["regime_affinity"] == pytest.approx(0.8)
        assert scores["correlation_score"] == pytest.approx(0.5)
        assert scores["regime_label"] == "bull"
        assert scores["crisis"] is False
        assert scores["concerns"] == []
        assert fake_screener.regime_affinity.call_count == 5

    def test_stage_sentiment_fundamentals_with_mocked_provider(self, monkeypatch):
        db = _memory_db()
        monkeypatch.setattr(
            "app.foundation.market.fundamentals",
            lambda _db, _symbol: {
                "data": {
                    "pe_ratio": 15.0,
                    "pb_ratio": 1.5,
                    "roe": 0.20,
                    "debt_equity": 50.0,
                    "market_cap": 1_000_000_000.0,
                    "revenue_growth": 0.10,
                },
                "source": "fake",
                "stale": False,
            },
        )

        class FakeRegistry:
            def get_analyst_estimates(self, symbol: str) -> dict:
                return {
                    "ok": True,
                    "provider": "fake",
                    "data": {"target_high": 150.0, "target_median": 120.0, "current_price": 100.0},
                }

        monkeypatch.setattr(
            "app.decision.discover.pipeline.build_provider_registry",
            lambda _db: FakeRegistry(),
        )

        from app.foundation.models.entities import NewsItem

        db.add(
            NewsItem(
                id=uuid4().hex,
                title="Positive AAPL headline",
                url="http://example.com",
                source="test",
                ticker="AAPL",
                sentiment_score=Decimal("0.6"),
                sentiment_label="positive",
                published_at=datetime.now(UTC) - timedelta(days=5),
            )
        )
        db.commit()

        scores, reject = stage_sentiment_fundamentals(db, "AAPL")

        assert reject is None
        assert scores is not None
        assert scores["pe"] == pytest.approx(15.0)
        assert scores["pb"] == pytest.approx(1.5)
        assert scores["roe"] == pytest.approx(0.20)
        assert scores["de_ratio"] == pytest.approx(50.0)
        assert scores["market_cap"] == pytest.approx(1_000_000_000.0)
        assert scores["revenue_growth"] == pytest.approx(0.10)
        assert scores["fundamentals_score"] > 0.5
        assert scores["analyst_estimate_score"] == pytest.approx(0.9)
        assert scores["sentiment_score"] == pytest.approx(0.6)
        assert scores["social_mentions"] == 1
        assert scores["concerns"] == []


# ======================================================================
# DossierWriter
# ======================================================================


class TestDossierWriter:
    def test_dossier_output_model_defaults(self):
        dossier = DossierOutputModel(
            direction="long",
            conviction=0.85,
            horizon_months=6,
            thesis="Buy AAPL",
            key_risks=["Valuation risk"],
            signal_breakdown={"composite_score": 0.85},
        )
        assert dossier.estimate is True
        assert dossier.not_financial_advice is True

    def test_write_dossier_populates_structured_json(self, monkeypatch):
        db = _memory_db()
        candidate = {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "isin": None,
            "source": "screen_index",
            "scores_json": json.dumps({"composite": 0.75, "concerns": []}),
            "tradeable_json": json.dumps(
                {"teilfreistellung_class": "aktien", "domicile": "US"}
            ),
        }
        profile = {"user_id": "user1"}

        monkeypatch.setattr(
            "app.decision.discover.dossier_writer._local_llm_sync",
            lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
                {
                    "direction": "long",
                    "conviction": 0.75,
                    "horizon_months": 6,
                    "thesis": "Buy AAPL",
                    "key_risks": ["Valuation risk"],
                }
            ),
        )
        monkeypatch.setattr(
            "app.decision.discover.dossier_writer._fetch_fundamentals",
            lambda _symbol, _db: {"pe": 15.0, "source": "fake"},
        )
        monkeypatch.setattr(
            "app.decision.discover.dossier_writer._fetch_sentiment",
            lambda _symbol, _db: {
                "news_sentiment": 0.6,
                "social_mentions": 5,
                "sentiment_trend": "rising",
            },
        )

        result = write_dossier(db, candidate, profile)

        assert "dossier_id" in result
        assert "recommendation_id" in result
        dossier = result["dossier"]
        assert dossier["direction"] == "long"
        assert dossier["conviction"] == 0.75
        assert dossier["horizon_months"] == 6
        # No quant anchor in scores_json for this candidate, so
        # expected_return is deterministically None (never LLM-produced).
        assert dossier["expected_return"] is None
        assert dossier["thesis"] == "Buy AAPL"
        assert dossier["key_risks"] == ["Valuation risk"]
        assert dossier["signal_breakdown"]["composite_score"] == 0.75
        assert dossier["estimate"] is True
        assert dossier["not_financial_advice"] is True


# ======================================================================
# Composite scoring & full pipeline behaviour
# ======================================================================


class TestCompositeScoringAndPipeline:
    def test_composite_scoring_weights(self):
        """Verify the pipeline maps stage outputs into composite signals exactly
        as compute_weighted_composite documents (advisor-loop PR1 weights)."""
        from app.decision.discover.composite import compute_weighted_composite

        db = _memory_db()
        scores = {
            "alpha_miner": {"ic": 0.1, "icir": 0.2, "n_obs": 100, "concerns": []},
            "alpha_screener": {"regime_affinity": 0.8, "concerns": []},
            "sentiment_fundamentals": {
                "analyst_estimate_score": 0.7,
                "sentiment_score": 0.6,
                "fundamentals_score": 0.5,
                "pe": 18.0,
                "pb": 2.5,
                "roe": 0.15,
                "de_ratio": 80.0,
                "market_cap": 5e9,
                "revenue_growth": 0.08,
                "social_mentions": 12,
                "concerns": [],
            },
            "momentum_quality": {"momentum_12_1m": 0.15, "volatility_6m": 0.2, "concerns": []},
            "backtest_vs_benchmark": {"excess_return_annual": 0.05, "sharpe_delta": 0.2, "concerns": []},
            "verification_gate": {
                "sharpe": 0.5,
                "volatility": 0.2,
                "max_drawdown": 0.15,
                "concerns": [],
            },
            "quant_signals": {"cvar_95_daily": -0.02, "var_95_daily": -0.015, "concerns": []},
            "portfolio_fit": {"fit_score": 0.9, "concerns": []},
        }

        ic_score = 0.5 + 0.1 * 3.0 + 0.2 * 0.25  # 0.85
        momentum_score = 0.5 + 0.5 * math.tanh(0.15 / 0.5)  # single candidate: no rank
        vol_pen = 0.2 / 0.60
        cvar_pen = 0.02 / 0.06
        mdd_pen = 0.15 / 0.30
        risk_score = 1.0 - (0.4 * vol_pen + 0.3 * cvar_pen + 0.3 * mdd_pen)
        benchmark_score = 0.5 + 0.05 + max(-0.1, min(0.1, 0.02 * 0.2))
        expected_composite = round(
            compute_weighted_composite(
                {
                    "ic_icir": ic_score,
                    "analyst": 0.7,
                    "sentiment": 0.6,
                    "portfolio": 0.9,
                    "fundamentals": 0.5,
                    "momentum": momentum_score,
                    "risk": risk_score,
                    "benchmark": benchmark_score,
                },
                ic_data=scores["alpha_miner"],
            ),
            4,
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate

            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_history_ingest",
                    return_value=({"concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_alpha_miner",
                    return_value=(scores["alpha_miner"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_alpha_screener",
                    return_value=(scores["alpha_screener"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_sentiment_fundamentals",
                    return_value=(scores["sentiment_fundamentals"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_momentum_quality",
                    return_value=(scores["momentum_quality"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_backtest_vs_benchmark",
                    return_value=(scores["backtest_vs_benchmark"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_verification_gate",
                    return_value=(scores["verification_gate"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_quant_signals",
                    return_value=(scores["quant_signals"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_portfolio_fit",
                    return_value=(scores["portfolio_fit"], None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_estimate_revision_signal",
                    return_value=({"sue": None, "revision_momentum": None, "concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_insider_signal",
                    return_value=({"cluster_buy_score": None, "net_insider_flow_usd": None, "concerns": []}, None),
                )
            )

            gen = run_candidate_pipeline(
                db, "user1", [{"symbol": "AAPL", "source": "screen_index"}]
            )
            shortlist = _consume_pipeline(gen)

        assert len(shortlist) == 1
        assert shortlist[0]["symbol"] == "AAPL"
        assert shortlist[0]["composite_score"] == pytest.approx(expected_composite)

    def test_regime_gate_blocks_pipeline(self):
        db = _memory_db()
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = True
            mock_gate.context_dict.return_value = {"regime_label": "bear"}
            mock_gate_cls.return_value = mock_gate

            gen = run_candidate_pipeline(
                db, "user1", [{"symbol": "AAPL", "source": "screen_index"}]
            )
            shortlist = _consume_pipeline(gen)

        assert shortlist == []

    def test_rejected_candidate_still_aggregates_stage_concerns(self):
        """Stage concerns must reach the top level even when a LATER stage
        rejects the candidate: the UI Flags column reads scores_json.concerns,
        so an empty aggregation hides exactly the flags explaining rejection."""
        db = _memory_db()
        results: list[dict] = []
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate

            def _ok(concerns):
                return ({"concerns": concerns}, None)

            stage_returns = [
                ("stage_history_ingest", _ok([])),
                ("stage_alpha_miner", _ok(["alphacrafter_miner_unavailable"])),
                ("stage_alpha_screener", _ok([])),
                ("stage_sentiment_fundamentals", _ok([])),
                ("stage_momentum_quality", _ok(["weak 12m momentum -3.0%"])),
                # Candidate is REJECTED here; later stages never run.
                ("stage_backtest_vs_benchmark", (None, "benchmark data unavailable")),
            ]
            for name, ret in stage_returns:
                stack.enter_context(
                    patch(f"app.decision.discover.pipeline.{name}", return_value=ret)
                )

            gen = run_candidate_pipeline(
                db, "user1", [{"symbol": "AAPL", "source": "screen_index"}],
                results_out=results,
            )
            shortlist = _consume_pipeline(gen)

        assert shortlist == []
        rejected = [r for r in results if r["reject_stage"] == "backtest_vs_benchmark"]
        assert len(rejected) == 1
        agg = rejected[0]["concerns"]
        assert "alphacrafter_miner_unavailable" in agg
        assert "weak 12m momentum -3.0%" in agg
        assert rejected[0]["composite_score"] == 0.0

    def test_batch_resolves_security_master_before_stage_loop(self):
        """resolve_securities_batch is called once with the full candidate
        list's (symbol, isin) pairs, before the per-candidate stage loop runs
        -- proving Discover wires provider-sourced ISINs into the security
        master ahead of any PIT gvkey join attempt (stage_quant_signals /
        stage_ml_signal depend on this having already happened)."""
        db = _memory_db()
        calls: list[tuple] = []

        def _fake_batch(db_arg, candidates_arg, *, alias_source):
            calls.append((db_arg, list(candidates_arg), alias_source))

        class _StopAfterPreresolve:
            def raise_if_cancelled(self):
                raise RuntimeError("stop-after-preresolve")

        candidates = [
            {"symbol": "AAPL", "isin": "US0378331005", "source": "screen_index"},
            {"symbol": "SAP", "isin": None, "source": "screen_index"},
        ]

        with ExitStack() as stack:
            stack.enter_context(patch("app.decision.discover.pipeline._preingest_candidates"))
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate
            stack.enter_context(
                patch("app.decision.discover.pipeline.resolve_securities_batch", _fake_batch)
            )

            gen = run_candidate_pipeline(
                db, "user1", candidates, cancel_token=_StopAfterPreresolve()
            )
            with pytest.raises(RuntimeError, match="stop-after-preresolve"):
                list(gen)

        assert len(calls) == 1
        db_arg, resolved_candidates, alias_source = calls[0]
        assert db_arg is db
        assert resolved_candidates == [("AAPL", "US0378331005"), ("SAP", None)]
        assert alias_source == "discover_pipeline"


# ======================================================================
# Tradeability gate
# ======================================================================


class TestTradeabilityGate:

    def _seed_candidate(
        self, db, run_id: str, symbol: str, tradeable_json: str
    ) -> DiscoverCandidate:
        cand = DiscoverCandidate(
            id=uuid_pk(),
            run_id=run_id,
            symbol=symbol,
            name=f"Fake {symbol}",
            source="screen_index",
            tradeable_json=tradeable_json,
        )
        db.add(cand)
        db.commit()
        return cand

    def _run_gate(self, db, shortlisted: list[DiscoverCandidate]) -> list[DiscoverCandidate]:
        tradeable_shortlisted: list[DiscoverCandidate] = []
        for cand in shortlisted:
            tradeable = json.loads(cand.tradeable_json) if cand.tradeable_json else {}
            if tradeable.get("likely_tradeable") is not False:
                tradeable_shortlisted.append(cand)
            else:
                cand.status = "rejected"
                cand.reject_stage = "tradeability_gate"
                cand.reject_reason = "Not likely tradeable at DKB"
        return tradeable_shortlisted

    def test_tradeable_candidate_passes_gate(self):
        db = _memory_db()
        run = DiscoverRun(
            id=uuid_pk(),
            user_id="user1",
            status="in_progress",
        )
        db.add(run)
        db.commit()

        tradeable = self._seed_candidate(db, run.id, "AAPL", json.dumps({"likely_tradeable": True}))
        shortlisted = self._run_gate(db, [tradeable])
        db.commit()

        assert len(shortlisted) == 1
        assert shortlisted[0].symbol == "AAPL"
        assert tradeable.status == "pending"  # unchanged

    def test_not_tradeable_candidate_rejected(self):
        db = _memory_db()
        run = DiscoverRun(
            id=uuid_pk(),
            user_id="user1",
            status="in_progress",
        )
        db.add(run)
        db.commit()

        not_tradeable = self._seed_candidate(
            db, run.id, "AVOID", json.dumps({"likely_tradeable": False})
        )
        shortlisted = self._run_gate(db, [not_tradeable])
        db.commit()

        assert len(shortlisted) == 0
        assert not_tradeable.status == "rejected"
        assert not_tradeable.reject_stage == "tradeability_gate"
        assert not_tradeable.reject_reason == "Not likely tradeable at DKB"

    def test_mixed_gate_passes_tradeable_rejects_not(self):
        db = _memory_db()
        run = DiscoverRun(
            id=uuid_pk(),
            user_id="user1",
            status="in_progress",
        )
        db.add(run)
        db.commit()

        good = self._seed_candidate(db, run.id, "GOOD", json.dumps({"likely_tradeable": True}))
        bad = self._seed_candidate(db, run.id, "BAD", json.dumps({"likely_tradeable": False}))
        missing = self._seed_candidate(db, run.id, "MISS", json.dumps({}))

        shortlisted = self._run_gate(db, [good, bad, missing])
        db.commit()

        assert len(shortlisted) == 2
        assert {c.symbol for c in shortlisted} == {"GOOD", "MISS"}
        assert good.status == "pending"
        assert bad.status == "rejected"
        assert bad.reject_stage == "tradeability_gate"
        assert missing.status == "pending"