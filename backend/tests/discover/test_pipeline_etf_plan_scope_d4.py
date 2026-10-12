"""Discover's ETF lane: plan scope and no momentum for bonds/cash/gold (plan 2026-10-12, D4 / WP4d)."""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from conftest import _memory_db

from app.decision.discover.composite import derive_signals_from_scores
from app.decision.discover.pipeline import assign_momentum_ranks, run_candidate_pipeline
from app.foundation.settings import upsert_public_settings

_STAGES = [
    ("stage_history_ingest", {"concerns": []}),
    ("stage_alpha_miner", {"ic": 0.0, "icir": 0.0, "n_obs": 0, "concerns": []}),
    ("stage_alpha_screener", {"regime_affinity": 0.5, "concerns": []}),
    ("stage_sentiment_fundamentals", {"analyst_estimate_score": 0.5, "sentiment_score": 0.5,
                                      "fundamentals_score": 0.5, "concerns": []}),
    ("stage_backtest_vs_benchmark", {"excess_return_annual": 0.0, "sharpe_delta": 0.0, "concerns": []}),
    ("stage_verification_gate", {"sharpe": 0.2, "volatility": 0.05, "max_drawdown": 0.03, "concerns": []}),
    ("stage_quant_signals", {"instrument_type": "etf", "concerns": []}),
    ("stage_portfolio_fit", {"fit_score": 0.95, "concerns": []}),
    ("stage_estimate_revision_signal", {"sue": None, "revision_momentum": None, "dispersion": None, "concerns": []}),
    ("stage_insider_signal", {"cluster_buy_score": None, "net_insider_flow_usd": None, "concerns": []}),
]


def _run(db, candidates):
    results: list[dict] = []
    with ExitStack() as stack:
        stack.enter_context(patch("app.decision.discover.pipeline._preingest_candidates"))
        gate_cls = stack.enter_context(patch("app.decision.discover.pipeline.MacroRegimeGate"))
        gate = MagicMock()
        gate.blocks_discovery.return_value = False
        gate_cls.return_value = gate
        for name, ret in _STAGES:
            stack.enter_context(patch(f"app.decision.discover.pipeline.{name}",
                                      side_effect=lambda *a, _r=ret, **k: (dict(_r), None)))
        stack.enter_context(patch(
            "app.decision.discover.pipeline.stage_momentum_quality",
            side_effect=lambda *a, **k: ({"momentum_12_1m": 0.30, "volatility_6m": 0.05,
                                          "concerns": ["weak 12m momentum -5%"]}, None),
        ))
        gen = run_candidate_pipeline(db, "user1", candidates, results_out=results)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
    return {r["symbol"]: r for r in results}


FTSE100 = {"symbol": "IUSZ.DE", "name": "iShares Core FTSE 100 UCITS ETF GBP (Dist)", "isin": "IE0005042456",
           "source": "screen_etf"}
XEON = {"symbol": "XEON.DE", "name": "Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C", "isin": "LU0290358497",
        "source": "screen_etf"}


def test_index_outside_the_plan_is_rejected_whatever_its_fit():
    db = _memory_db()
    out = _run(db, [FTSE100])
    assert out["IUSZ.DE"]["reject_stage"] == "plan_scope"
    assert "information only" in out["IUSZ.DE"]["reject_reason"]


def test_money_market_without_a_cash_target_is_information_only():
    db = _memory_db()
    assert _run(db, [XEON])["XEON.DE"]["reject_stage"] == "plan_scope"


def test_money_market_with_a_cash_target_is_not_ranked_on_momentum():
    db = _memory_db()
    upsert_public_settings(db, {"plan_cash_target_pct": 5})
    res = _run(db, [XEON])["XEON.DE"]
    assert res["reject_stage"] is None
    mom = res["scores"]["momentum_quality"]
    assert mom["momentum_skipped"].startswith("money_market")
    assert not any("momentum" in c for c in mom["concerns"])
    signals = derive_signals_from_scores(res["scores"])
    assert signals["momentum"] == 0.5


def test_skipped_momentum_is_left_out_of_the_ranks():
    stocks = [{"momentum_quality": {"momentum_12_1m": v}} for v in (0.1, 0.2, 0.3, 0.4, 0.5)]
    gold = {"momentum_quality": {"momentum_12_1m": 0.9, "momentum_skipped": "gold: not ranked on momentum"}}
    assign_momentum_ranks([*stocks, gold])
    assert "momentum_rank" not in gold["momentum_quality"]
