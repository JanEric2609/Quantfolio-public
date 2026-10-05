"""Tests for the LLM factor proposer: db is forwarded, unsafe formulas discarded."""

import asyncio
import json
from types import SimpleNamespace

import app.lab.alphacrafter.llm_factor_proposer as proposer_mod
from app.lab.alphacrafter.llm_factor_proposer import (
    LLMFactorProposal,
    propose_factors_via_llm,
    proposals_to_factor_defs,
)


class _FakeRAG:
    def __init__(self, db):
        self.db = db

    async def retrieve(self, **kwargs):
        return []


def _install_fakes(monkeypatch, llm_response_text):
    captured = {}

    async def fake_call(db, task_type, messages, *, force_local=False, timeout_s=60.0, structured_output=None):
        captured["db"] = db
        captured["task_type"] = task_type
        captured["force_local"] = force_local
        # Completion.content is a plain str (see app/services/llm/base.py); the
        # previous list-of-blocks shape never matched the real model.
        return SimpleNamespace(content=llm_response_text)

    monkeypatch.setattr(proposer_mod, "llm_call", fake_call)
    monkeypatch.setattr(proposer_mod, "RAGRetriever", _FakeRAG)
    return captured


def test_proposer_forwards_db_and_filters_unsafe(monkeypatch):
    response = json.dumps(
        [
            {
                "name": "good_mom",
                "description": "cross-sectional momentum",
                "formula": "rank(delta(close, 5))",
                "intuition": "winners keep winning",
                "expected_regime": "bull",
            },
            {
                "name": "evil",
                "description": "malicious",
                "formula": "__import__('os').system('echo hi')",
                "intuition": "bad",
            },
            {
                "name": "attr_access",
                "description": "also bad",
                "formula": "close.values",
                "intuition": "bad",
            },
        ]
    )
    captured = _install_fakes(monkeypatch, response)
    sentinel_db = object()

    proposals, reasoning = asyncio.run(
        propose_factors_via_llm(
            sentinel_db,
            universe_stats={"n_stocks": 5, "n_days": 100},
            regime_label="bull",
            force_local=True,
        )
    )

    # db forwarded to the router; correct task type.
    assert captured["db"] is sentinel_db
    assert captured["task_type"] == "batch_research"
    assert captured["force_local"] is True

    # Only the safe, DSL-valid formula survives.
    assert len(proposals) == 1
    assert proposals[0].name == "good_mom"
    assert proposals[0].dsl == "rank(delta(close, 5))"


def test_proposer_handles_unparseable_response(monkeypatch):
    _install_fakes(monkeypatch, "I cannot help with that.")
    proposals, reasoning = asyncio.run(
        propose_factors_via_llm(object(), universe_stats={}, regime_label=None)
    )
    assert proposals == []
    assert "Failed to parse" in reasoning


def test_proposals_to_factor_defs():
    proposals = [
        LLMFactorProposal(name="f1", description="", dsl="rank(close)", intuition=""),
    ]
    defs = proposals_to_factor_defs(proposals)
    assert defs == [{"name": "f1", "formula": "rank(close)", "source": "llm", "dsl": "rank(close)"}]
