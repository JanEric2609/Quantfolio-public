"""Dossier defects found by the 2026-10-26 prod check (web cross-check of 15 dossiers)."""
from __future__ import annotations

import json
from unittest.mock import patch

from conftest import _memory_db

from app.decision.discover import pipeline
from app.decision.discover.dossier_writer import _grounding_prompt_lines, write_dossier
from app.decision.discover.pipeline import stage_sentiment_fundamentals
from app.decision.discover.thesis_checks import check_thesis, ticker_problem


def test_near_miss_ticker_is_rejected():
    facts = "Candidate: DDOG (Datadog)\n"
    assert ticker_problem("DDDOG offers moderate portfolio fit.", "DDOG", facts) is not None
    assert ticker_problem("DDOG offers moderate portfolio fit.", "DDOG", facts) is None
    # A truncation is a substring of the real ticker in the facts, but not a word there.
    assert ticker_problem("DDO offers moderate portfolio fit.", "DDOG", facts) is not None
    # Short tickers and unrelated capitals (EUR, ETF) are left alone.
    assert ticker_problem("MUU and EUR exposure.", "MU", facts) is None
    assert ticker_problem("Priced in EUR with ETF peers.", "HSBA.L", facts) is None

    check = check_thesis(
        "DDDOG has strong momentum.", [], facts_text=facts, macro=None, direction="long", symbol="DDOG",
    )
    assert check.reason == "ticker"


def test_momentum_return_and_rank_are_named_apart():
    scores = {"momentum_quality": {"momentum_12_1m": 1.3307, "momentum_rank": 0.963}}
    text = _grounding_prompt_lines(scores, {"anchor_kind": "trailing_3y_annualized_return", "components": {}})
    assert "+133.1% price return" in text
    assert "momentum rank is 0.96" in text
    assert "never write either as a multiple" in text


def test_portfolio_fit_is_a_correlation_to_the_portfolio_not_a_style():
    scores = {"portfolio_fit": {
        "correlation": 0.5623, "correlation_weeks": 97, "weight_basis": "market_value", "fit_score": 0.5313,
    }}
    text = _grounding_prompt_lines(scores, {})
    assert "correlation 0.56 with the investor's current portfolio" in text
    assert "weighted by its market value, 97 weeks" in text
    assert "not to an index" in text


def test_unclassified_bank_loses_its_revenue_growth():
    """HSBA.L: Finnhub had no profile, so the financials check never saw a sector."""
    db = _memory_db()
    data = {"revenue_growth": 0.254, "pe_ratio": 13.0}
    fund = {"ticker": "HSBA.L", "data": data, "source": "finnhub", "stale": False}
    with (
        patch("app.foundation.market.fundamentals", return_value=fund),
        patch.object(pipeline, "_yfinance_classification", return_value=("Financial Services", "Banks - Diversified")),
    ):
        scores, _ = stage_sentiment_fundamentals(db, "HSBA.L", is_etf=True)
    assert scores is not None
    assert scores["revenue_growth"] is None
    assert scores["sector"] == "Financial Services"
    assert "revenue_growth_unreliable" in scores["concerns"]


def test_dossier_prompt_uses_the_checked_revenue_growth(monkeypatch):
    monkeypatch.setenv("DISCOVER_DOSSIER_SLOTS", "0")  # the old prompt's score dump
    db = _memory_db()
    seen: list[str] = []

    def _llm(_db, messages, timeout_s=120.0, **_kw):
        seen.append(messages[1]["content"])
        return json.dumps({"thesis": "HSBA trades at a P/E of 13.0.", "key_risks": []})

    monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync", _llm)
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: {"pe": 13.0, "revenue_growth": 0.254, "sector": None},
    )
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_sentiment", lambda _symbol, _db: {})
    scores = {
        "composite": 0.54,
        "sentiment_fundamentals": {
            "revenue_growth": None, "sector": "Financial Services", "concerns": ["revenue_growth_unreliable"],
        },
    }
    candidate = {"symbol": "HSBA.L", "name": "HSBC", "isin": None, "source": "screen",
                 "scores_json": json.dumps(scores), "tradeable_json": "{}"}
    write_dossier(db, candidate, {"user_id": "u1"})

    prompt = seen[0]
    assert '"revenue_growth": 0.254' not in prompt
    assert '"sector": "Financial Services"' in prompt
    assert "Ticker: write it exactly as HSBA." in prompt
    assert "Portfolio context:" not in prompt


def test_slot_prompt_uses_the_checked_revenue_growth(monkeypatch):
    db = _memory_db()
    seen: list[str] = []

    def _llm(_db, messages, timeout_s=120.0, **_kw):
        seen.append(messages[1]["content"])
        return json.dumps({"thesis": "HSBA trades at {pe}. It is a bank.", "key_risks": []})

    monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync", _llm)
    monkeypatch.setattr(
        "app.decision.discover.dossier_writer._fetch_fundamentals",
        lambda _symbol, _db: {"pe": 13.0, "revenue_growth": 0.254, "sector": None},
    )
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_sentiment", lambda _symbol, _db: {})
    scores = {
        "composite": 0.54,
        "sentiment_fundamentals": {
            "revenue_growth": None, "sector": "Financial Services", "concerns": ["revenue_growth_unreliable"],
        },
    }
    candidate = {"symbol": "HSBA.L", "name": "HSBC", "isin": None, "source": "screen",
                 "scores_json": json.dumps(scores), "tradeable_json": "{}"}
    out = write_dossier(db, candidate, {"user_id": "u1"})

    prompt = seen[0]
    # A bank's gross interest income is no revenue growth: no fact, no figure.
    assert "{revenue_growth}" not in prompt and "25.4" not in prompt
    assert "Sector: Financial Services" in prompt
    assert "{pe}: a trailing price-to-earnings ratio of 13.0" in prompt
    assert "Ticker: write it exactly as HSBA." in prompt
    assert out["dossier"]["thesis"] == "HSBA trades at a trailing price-to-earnings ratio of 13.0. It is a bank."


def _write_with(monkeypatch, llm_json):
    db = _memory_db()
    monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync",
                        lambda _db, messages, timeout_s=120.0, **_kw: json.dumps(llm_json))
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_fundamentals",
                        lambda _symbol, _db: {"pe": 13.0, "sector": None})
    monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_sentiment", lambda _symbol, _db: {})
    scores = {"composite": 0.54, "sentiment_fundamentals": {"sector": "Financial Services"},
              "verification_gate": {"volatility": 0.21, "max_drawdown": 0.1}}
    candidate = {"symbol": "HSBA.L", "name": "HSBC", "isin": None, "source": "screen",
                 "scores_json": json.dumps(scores), "tradeable_json": "{}"}
    return write_dossier(db, candidate, {"user_id": "u1"})


def test_a_one_sentence_thesis_on_one_fact_is_not_accepted(monkeypatch):
    """WP8: CA.PA, XDEM and MTDR had a single reason; two grounded reasons or the code-written thesis."""
    out = _write_with(monkeypatch, {"thesis": "HSBA trades at {pe}.", "key_risks": []})
    assert out["dossier"]["thesis"] != "HSBA trades at a trailing price-to-earnings ratio of 13.0."


def test_risks_citing_the_same_figure_are_one_risk(monkeypatch):
    out = _write_with(monkeypatch, {
        "thesis": "HSBA trades at {pe}. It is a bank.",
        "key_risks": ["The stock has {volatility}.", "Volatility may hurt: {volatility}."],
    })
    volatility = [r for r in out["dossier"]["key_risks"] if "volatility" in r.lower()]
    assert len(volatility) <= 1
